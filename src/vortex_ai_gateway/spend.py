"""The per-key ledger: what each caller has used, and what it cost.

:mod:`vortex_ai_gateway.streaming` already establishes that a finished request
knows its own token counts. This is where those counts stop being a log line
nobody adds up and become a number a caller can read back.

**The ledger stores tokens, not money.** Three reasons, and the third is the one
that matters. Token counts are integers, so ``HINCRBY`` is exact where
``HINCRBYFLOAT`` is not. They are what the vendor's invoice is itemised by, so a
disagreement is traceable. And they do not go stale: a price correction reprices
the whole history the next time someone asks, where stored dollars would freeze
each request at whatever the table said on the day, and a wrong price would need
a migration to fix rather than an edit (ADR-022).

**Recording never fails a request.** By the time this runs the provider has
already answered and the caller already has their tokens. A Redis outage that
turned a served request into a 500 would lose the answer *and* the record; a
Redis outage that drops the record loses only the record, and says so.

Keys are ``vortex:usage:<key_id>:<YYYY-MM-DD>`` — one hash per key per UTC day,
holding three fields per model. A day is the coarsest bucket that still answers
"what changed yesterday?", and a hash per day is what makes retention a TTL
rather than a sweep.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from vortex_ai_gateway.contracts import DailyUsage, ModelUsage, UsageReport
from vortex_ai_gateway.pricing import PriceTable

logger = structlog.get_logger(__name__)

#: One line per settled request, whatever it cost and however it ended. The
#: ledger is the queryable form; this is the durable one, and it is written even
#: when Redis is not there to take the increment.
SETTLED_EVENT: Final = "request settled"
DEGRADED_EVENT: Final = "usage ledger unavailable; request not recorded"

#: Separates the model from the quantity inside a day's hash. A model name may
#: contain slashes, dots and dashes; it may not contain this.
FIELD_SEPARATOR: Final = "|"

#: The four quantities kept per model. `requests` is here because tokens alone
#: cannot distinguish one enormous call from a thousand small ones, and the two
#: are charged the same but behave nothing alike. `unmetered` counts the subset
#: whose usage never arrived — see :class:`~vortex_ai_gateway.contracts.spend.ModelUsage`.
REQUESTS: Final = "requests"
UNMETERED: Final = "unmetered"
PROMPT: Final = "prompt"
COMPLETION: Final = "completion"


def _day(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class _ModelTotals:
    """One model's counters for one day, before they meet a price."""

    requests: int = 0
    unmetered: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def plus(self, quantity: str, amount: int) -> _ModelTotals:
        """This day's counters with one field replaced by the stored total."""
        replacements = {quantity: amount}
        return _ModelTotals(
            requests=replacements.get(REQUESTS, self.requests),
            unmetered=replacements.get(UNMETERED, self.unmetered),
            prompt_tokens=replacements.get(PROMPT, self.prompt_tokens),
            completion_tokens=replacements.get(COMPLETION, self.completion_tokens),
        )


class SpendLedger:
    """Per-key token counters in Redis, priced on the way out."""

    def __init__(
        self,
        redis: Redis,
        prices: PriceTable,
        *,
        retention_days: int = 30,
        prefix: str = "vortex:usage",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._prices = prices
        self.retention_days = max(1, retention_days)
        self.prefix = prefix
        self._clock = clock

    def _key(self, key_id: str, day: str) -> str:
        return f"{self.prefix}:{key_id}:{day}"

    async def record(
        self,
        key_id: str,
        model: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        metered: bool = True,
    ) -> None:
        """Add one request's usage to today's totals for ``key_id``.

        ``metered=False`` records that the request happened and that nobody
        knows what it cost — an abandoned stream, or a provider that answered
        without usage. It is counted, and counted *separately*, because the
        alternative is a report that quietly calls it free.

        Written as a pipeline rather than four round trips. Not a transaction:
        the fields belong to the same request, but nothing reads a partial write
        as anything other than a slightly stale total, and a partial write is
        already the best available outcome when Redis dies mid-call.
        """
        logger.info(
            SETTLED_EVENT,
            api_key_id=key_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            metered=metered,
        )
        day = _day(self._clock())
        key = self._key(key_id, day)
        try:
            pipeline = self._redis.pipeline(transaction=False)
            pipeline.hincrby(key, f"{model}{FIELD_SEPARATOR}{REQUESTS}", 1)
            pipeline.hincrby(key, f"{model}{FIELD_SEPARATOR}{UNMETERED}", 0 if metered else 1)
            pipeline.hincrby(key, f"{model}{FIELD_SEPARATOR}{PROMPT}", prompt_tokens)
            pipeline.hincrby(key, f"{model}{FIELD_SEPARATOR}{COMPLETION}", completion_tokens)
            # Refreshed on every write, so a key that is used every day keeps a
            # rolling window and one that goes quiet expires on its own.
            pipeline.expire(key, timedelta(days=self.retention_days))
            await pipeline.execute()
        except (RedisError, OSError) as exc:
            logger.warning(DEGRADED_EVENT, api_key_id=key_id, model=model, error=str(exc))

    async def report(self, key_id: str, *, days: int) -> UsageReport:
        """Everything the ledger knows about ``key_id`` over the last ``days``.

        Today counts as one of them, so ``days=1`` is "today so far" rather than
        "yesterday" — which is what a caller checking whether they are about to
        run out actually wants.
        """
        window = max(1, min(days, self.retention_days))
        now = self._clock()
        wanted = [_day(now - offset * 86_400) for offset in reversed(range(window))]

        try:
            pipeline = self._redis.pipeline(transaction=False)
            for day in wanted:
                pipeline.hgetall(self._key(key_id, day))
            raw: list[Mapping[bytes | str, bytes | str]] = await pipeline.execute()
        except (RedisError, OSError) as exc:
            logger.warning(DEGRADED_EVENT, api_key_id=key_id, error=str(exc))
            raw = [{} for _ in wanted]

        daily = [self._day_report(day, fields) for day, fields in zip(wanted, raw, strict=True)]
        return self._summarise(key_id, wanted, daily)

    def _day_report(self, day: str, fields: Mapping[bytes | str, bytes | str]) -> DailyUsage:
        """Turn one day's flat hash back into per-model lines, priced."""
        totals: dict[str, _ModelTotals] = {}
        for raw_field, raw_value in fields.items():
            field = raw_field.decode() if isinstance(raw_field, bytes) else raw_field
            value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
            model, _, quantity = field.rpartition(FIELD_SEPARATOR)
            if not model:  # pragma: no cover - only reachable if something else writes here
                continue
            totals[model] = totals.get(model, _ModelTotals()).plus(quantity, int(value))

        models = [
            ModelUsage(
                model=model,
                requests=counts.requests,
                unmetered_requests=counts.unmetered,
                prompt_tokens=counts.prompt_tokens,
                completion_tokens=counts.completion_tokens,
                total_tokens=counts.prompt_tokens + counts.completion_tokens,
                cost_usd=self._prices.cost(model, counts.prompt_tokens, counts.completion_tokens),
            )
            for model, counts in sorted(totals.items())
        ]
        priced = [line.cost_usd for line in models if line.cost_usd is not None]
        return DailyUsage(
            date=day,
            requests=sum(line.requests for line in models),
            unmetered_requests=sum(line.unmetered_requests for line in models),
            total_tokens=sum(line.total_tokens for line in models),
            cost_usd=sum(priced, Decimal(0)) if priced else None,
            models=models,
        )

    def _summarise(self, key_id: str, days: list[str], daily: list[DailyUsage]) -> UsageReport:
        """Roll the days up, keeping track of what could not be priced."""
        priced = [day.cost_usd for day in daily if day.cost_usd is not None]
        unpriced = {line.model for day in daily for line in day.models if line.cost_usd is None}
        return UsageReport(
            key_id=key_id,
            start_date=days[0],
            end_date=days[-1],
            total_requests=sum(day.requests for day in daily),
            total_unmetered_requests=sum(day.unmetered_requests for day in daily),
            total_tokens=sum(day.total_tokens for day in daily),
            total_cost_usd=sum(priced, Decimal(0)) if priced else None,
            unpriced_models=sorted(unpriced),
            daily=daily,
        )
