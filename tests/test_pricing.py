"""Tests for the price table.

Almost every assertion here is about a distinction that a sloppier
implementation collapses: unpriced is not free, zero is not unknown, and a
price read through `float` is not the price the operator typed.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.pricing import (
    DEFAULT_PRICES,
    ModelPrice,
    PriceTable,
    PriceTableError,
)


def table(**models: tuple[str, str]) -> PriceTable:
    return PriceTable(
        (name, ModelPrice(prompt=Decimal(prompt), completion=Decimal(completion)))
        for name, (prompt, completion) in models.items()
    )


def test_cost_is_per_million_tokens() -> None:
    """$2.50 per million prompt tokens is $2.50 for a million prompt tokens."""
    price = ModelPrice(prompt=Decimal("2.50"), completion=Decimal("10.00"))

    assert price.cost(1_000_000, 0) == Decimal("2.50")
    assert price.cost(0, 1_000_000) == Decimal("10.00")


def test_cost_is_exact_where_a_float_would_not_be() -> None:
    """The reason this module is written in Decimal and not in `float`.

    A tenth of a cent per thousand requests is the scale at which "close enough"
    stops being close enough, and binary floating point cannot hold 0.15/1e6 at
    all.
    """
    price = ModelPrice(prompt=Decimal("0.15"), completion=Decimal("0.60"))

    total = sum((price.cost(1_000, 500) for _ in range(1_000)), Decimal(0))
    drifted = 0.0
    for _ in range(1_000):
        drifted += 0.15 * 1_000 / 1e6 + 0.60 * 500 / 1e6

    assert total == Decimal("0.450")
    # A thousand identical requests, and the float sum is already wrong in the
    # thirteenth place: 0.450000000000005. Small, until it is a million requests
    # and someone has to explain the difference to a customer.
    assert drifted != 0.45
    assert float(total) == 0.45


def test_the_first_matching_pattern_wins() -> None:
    """Ordered, like the routing table, so a specific rule can precede a general one."""
    prices = table(**{"gpt-4o-mini*": ("0.15", "0.60"), "gpt-4o*": ("2.50", "10.00")})

    assert prices.price_of("gpt-4o-mini-2024") == ModelPrice(Decimal("0.15"), Decimal("0.60"))
    assert prices.price_of("gpt-4o-2024") == ModelPrice(Decimal("2.50"), Decimal("10.00"))


def test_an_unpriced_model_costs_none_not_zero() -> None:
    """An unpriced model billed as free is how one gets rolled out to nobody's invoice."""
    prices = table(**{"gpt-4o*": ("2.50", "10.00")})

    assert prices.cost("some-new-model", 1_000, 1_000) is None


def test_a_free_model_costs_zero_not_none() -> None:
    """Self-hosted really is free, and that is a different claim from "unknown"."""
    assert PriceTable(DEFAULT_PRICES).cost("local/llama3", 10_000, 10_000) == Decimal(0)


def test_the_builtin_table_prices_the_models_the_router_knows() -> None:
    """A routing table that names a model the price table cannot is a silent gap."""
    prices = PriceTable(DEFAULT_PRICES)

    for model in ("gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-latest", "local/llama3"):
        assert prices.price_of(model) is not None, model


# --- the operator's override file --------------------------------------------


def write_table(tmp_path: Path, content: object) -> str:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(content) if not isinstance(content, str) else content)
    return str(path)


def test_a_file_overrides_the_builtin_price(tmp_path: Path) -> None:
    """A vendor price change is an edit, not a release."""
    path = write_table(tmp_path, {"gpt-4o*": {"prompt": 1.25, "completion": 5.0}})
    prices = PriceTable.from_settings(Settings(_env_file=None, price_table_path=path))

    assert prices.cost("gpt-4o", 1_000_000, 0) == Decimal("1.25")


def test_an_override_need_only_name_what_it_corrects(tmp_path: Path) -> None:
    """The built-ins keep serving everything the file is silent about."""
    path = write_table(tmp_path, {"gpt-4o*": {"prompt": 1.25, "completion": 5.0}})
    prices = PriceTable.from_settings(Settings(_env_file=None, price_table_path=path))

    assert prices.price_of("claude-3-5-sonnet-latest") is not None


def test_prices_are_read_through_decimal_not_float(tmp_path: Path) -> None:
    """0.15 in the file is 0.15 in the table, not the nearest binary double."""
    path = write_table(tmp_path, {"x": {"prompt": 0.15, "completion": 0.15}})
    prices = PriceTable.from_settings(Settings(_env_file=None, price_table_path=path))
    price = prices.price_of("x")

    assert price is not None
    assert price.prompt == Decimal("0.15")


def test_no_file_configured_is_the_builtin_table() -> None:
    prices = PriceTable.from_settings(Settings(_env_file=None))

    assert prices.price_of("gpt-4o") is not None


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{not json", "unparseable"),
        ([1, 2, 3], "not an object"),
        ({"x": 3}, "price is not an object"),
        ({"x": {"prompt": 1}}, "missing the completion half"),
        ({"x": {"prompt": "free", "completion": 1}}, "not a number"),
        ({"x": {"prompt": -1, "completion": 1}}, "negative"),
    ],
)
def test_an_unreadable_table_stops_the_gateway(tmp_path: Path, content: object, why: str) -> None:
    """A price table that silently reverts to defaults is a billing bug that reports nothing."""
    path = write_table(tmp_path, content)

    with pytest.raises(PriceTableError):
        PriceTable.from_settings(Settings(_env_file=None, price_table_path=path))


def test_a_missing_file_stops_the_gateway(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, price_table_path=str(tmp_path / "absent.json"))

    with pytest.raises(PriceTableError):
        PriceTable.from_settings(settings)
