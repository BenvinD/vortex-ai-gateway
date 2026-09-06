"""What a completion costs, in money.

The gateway already knows what every request *spent* — :mod:`streaming` writes a
token count for streams and the buffered path has one in the response body. This
turns that into dollars, and it is the only module in the codebase that deals in
money at all.

Two rules it keeps, both learned the expensive way by everyone who has not:

**Prices are per million tokens, arithmetic is in :class:`~decimal.Decimal`.**
A price like ``$2.50 / 1M`` is 0.0000025 dollars a token, and a float cannot
hold that exactly. Summing a few million of them in binary floating point
produces a number that is *nearly* the invoice, which is the worst kind of
wrong: close enough that nobody checks it and different enough to argue about.

**A model with no price is not free.** ``price_of`` returns ``None`` rather than
zero, and everything downstream reports the tokens with the cost left unknown.
A missing row in the table is a gap in the table, and an unpriced model silently
costing nothing is how a new model gets rolled out and billed to no one.

Patterns are shell globs, matched in order, exactly as
:mod:`vortex_ai_gateway.routing` matches models to providers — one syntax for
"which models does this rule mean", used twice (ADR-022).
"""

import fnmatch
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import structlog

from vortex_ai_gateway.config import Settings

logger = structlog.get_logger(__name__)

#: What the table is denominated in. Vendors publish per-million prices, so the
#: table holds what the vendor's own page says and the conversion happens once,
#: here, rather than in whoever transcribed it.
TOKENS_PER_UNIT: Final = Decimal(1_000_000)

PRICES_LOADED_EVENT: Final = "price table loaded"
UNPRICED_EVENT: Final = "model has no price"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million prompt and completion tokens, as the vendor publishes it."""

    prompt: Decimal
    completion: Decimal

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        """What those token counts cost, in USD.

        Not rounded. Rounding belongs at the point something is *presented* or
        *charged*, and doing it here would round every request independently and
        then sum the errors.
        """
        return (self.prompt * prompt_tokens + self.completion * completion_tokens) / TOKENS_PER_UNIT


#: The built-in table: a starting point, not a source of truth. Vendor prices
#: change more often than this repository does, which is exactly why
#: ``VORTEX_PRICE_TABLE_PATH`` exists — an operator corrects a price by editing
#: a JSON file, not by waiting for a release. Ordered most specific first,
#: because the first matching pattern wins.
DEFAULT_PRICES: Final[tuple[tuple[str, ModelPrice], ...]] = (
    ("gpt-4o-mini*", ModelPrice(prompt=Decimal("0.15"), completion=Decimal("0.60"))),
    ("gpt-4o*", ModelPrice(prompt=Decimal("2.50"), completion=Decimal("10.00"))),
    ("gpt-4.1-mini*", ModelPrice(prompt=Decimal("0.40"), completion=Decimal("1.60"))),
    ("gpt-4.1*", ModelPrice(prompt=Decimal("2.00"), completion=Decimal("8.00"))),
    ("claude-*-haiku*", ModelPrice(prompt=Decimal("0.80"), completion=Decimal("4.00"))),
    ("claude-*-sonnet*", ModelPrice(prompt=Decimal("3.00"), completion=Decimal("15.00"))),
    ("claude-*-opus*", ModelPrice(prompt=Decimal("15.00"), completion=Decimal("75.00"))),
    # Anything served from our own hardware has no per-token invoice behind it.
    # Zero here is a real price, not a missing one — which is the distinction
    # `price_of` returning `None` exists to preserve.
    ("local/*", ModelPrice(prompt=Decimal(0), completion=Decimal(0))),
    ("mock-*", ModelPrice(prompt=Decimal(0), completion=Decimal(0))),
)


class PriceTable:
    """Model-name patterns to prices, first match wins."""

    def __init__(self, prices: Iterable[tuple[str, ModelPrice]]) -> None:
        self._prices = tuple(prices)

    @classmethod
    def from_settings(cls, settings: Settings) -> PriceTable:
        """The built-in table, with an operator's file layered in front of it.

        In front, not merged over: the file's rules are consulted first, so an
        override needs only the models it is correcting and the built-ins keep
        serving everything else. A file that cannot be read stops the gateway at
        startup rather than being ignored — a price table silently reverting to
        defaults is a billing bug that reports nothing.
        """
        overrides = _load_price_file(settings.price_table_path)
        table = cls((*overrides, *DEFAULT_PRICES))
        logger.info(
            PRICES_LOADED_EVENT,
            overrides=len(overrides),
            builtin=len(DEFAULT_PRICES),
            path=settings.price_table_path or None,
        )
        return table

    def price_of(self, model: str) -> ModelPrice | None:
        """The first rule matching ``model``, or ``None`` if the table is silent."""
        for pattern, price in self._prices:
            if fnmatch.fnmatchcase(model, pattern):
                return price
        return None

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
        """What this request cost, or ``None`` when the model has no price."""
        price = self.price_of(model)
        if price is None:
            logger.warning(UNPRICED_EVENT, model=model)
            return None
        return price.cost(prompt_tokens, completion_tokens)


class PriceTableError(ValueError):
    """The configured price table cannot be read, and the gateway will not start."""


def _load_price_file(path: str) -> tuple[tuple[str, ModelPrice], ...]:
    """Parse ``{"gpt-4o": {"prompt": 2.5, "completion": 10.0}}`` into rules.

    Numbers are read through ``str`` into :class:`~decimal.Decimal` rather than
    through :func:`float`, so ``0.15`` in the file is ``0.15`` in the table
    instead of the nearest binary approximation to it.
    """
    if not path:
        return ()
    file = Path(path)
    try:
        raw = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PriceTableError(f"Cannot read the price table at {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PriceTableError(f"The price table at {path} must be an object of model -> prices.")

    rules: list[tuple[str, ModelPrice]] = []
    for pattern, entry in raw.items():
        if not isinstance(entry, Mapping) or not {"prompt", "completion"} <= set(entry):
            raise PriceTableError(
                f"Price for '{pattern}' must be an object with 'prompt' and 'completion'."
            )
        try:
            price = ModelPrice(
                prompt=Decimal(str(entry["prompt"])),
                completion=Decimal(str(entry["completion"])),
            )
        except ArithmeticError as exc:
            raise PriceTableError(f"Price for '{pattern}' is not a number: {exc}") from exc
        if price.prompt < 0 or price.completion < 0:
            raise PriceTableError(f"Price for '{pattern}' is negative.")
        rules.append((pattern, price))
    return tuple(rules)
