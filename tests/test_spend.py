"""Tests for the per-key usage ledger and the report it produces."""

from decimal import Decimal

import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from vortex_ai_gateway.pricing import DEFAULT_PRICES, ModelPrice, PriceTable
from vortex_ai_gateway.spend import SpendLedger

#: 2026-09-06T12:00:00Z, and 2026-09-05T12:00:00Z one day earlier — a fixed
#: instant so "today" is an assertion rather than whatever the clock says.
TODAY = 1_788_696_000.0
DAY = 86_400.0


class Clock:
    def __init__(self, now: float = TODAY) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def redis() -> aioredis.FakeRedis:
    return aioredis.FakeRedis()


@pytest.fixture
def moment() -> Clock:
    return Clock()


@pytest.fixture
def ledger(redis: aioredis.FakeRedis, moment: Clock) -> SpendLedger:
    return SpendLedger(redis, PriceTable(DEFAULT_PRICES), clock=moment)


# --- recording ---------------------------------------------------------------


async def test_usage_accumulates_across_requests(ledger: SpendLedger) -> None:
    for _ in range(3):
        await ledger.record("k-1", "gpt-4o", prompt_tokens=100, completion_tokens=50)

    report = await ledger.report("k-1", days=1)

    assert report.total_requests == 3
    assert report.total_tokens == 450
    assert report.daily[0].models[0].prompt_tokens == 300
    assert report.daily[0].models[0].completion_tokens == 150


async def test_models_are_kept_apart(ledger: SpendLedger) -> None:
    """A per-model breakdown is what makes a bill reconcilable against an invoice."""
    await ledger.record("k-1", "gpt-4o", prompt_tokens=100, completion_tokens=100)
    await ledger.record("k-1", "gpt-4o-mini", prompt_tokens=100, completion_tokens=100)

    lines = (await ledger.report("k-1", days=1)).daily[0].models

    assert [line.model for line in lines] == ["gpt-4o", "gpt-4o-mini"]
    assert all(line.total_tokens == 200 for line in lines)


async def test_keys_are_kept_apart(ledger: SpendLedger) -> None:
    await ledger.record("k-1", "gpt-4o", prompt_tokens=100, completion_tokens=0)
    await ledger.record("k-2", "gpt-4o", prompt_tokens=999, completion_tokens=0)

    assert (await ledger.report("k-1", days=1)).total_tokens == 100


async def test_the_ledger_stores_integers_not_money(
    ledger: SpendLedger, redis: aioredis.FakeRedis
) -> None:
    """Prices change; what was used does not. Repricing must not need a migration."""
    await ledger.record("k-1", "gpt-4o", prompt_tokens=1_000, completion_tokens=500)

    stored = await redis.hgetall("vortex:usage:k-1:2026-09-06")

    assert stored == {
        b"gpt-4o|requests": b"1",
        b"gpt-4o|unmetered": b"0",
        b"gpt-4o|prompt": b"1000",
        b"gpt-4o|completion": b"500",
    }


async def test_a_days_counters_expire(ledger: SpendLedger, redis: aioredis.FakeRedis) -> None:
    """Retention is a TTL, so nothing has to sweep."""
    await ledger.record("k-1", "gpt-4o", prompt_tokens=1, completion_tokens=1)

    assert await redis.ttl("vortex:usage:k-1:2026-09-06") == 30 * 86_400


# --- pricing on the way out --------------------------------------------------


async def test_the_report_prices_what_it_read(ledger: SpendLedger) -> None:
    """$2.50/1M prompt and $10.00/1M completion, for 1000 and 500 tokens."""
    await ledger.record("k-1", "gpt-4o", prompt_tokens=1_000, completion_tokens=500)

    report = await ledger.report("k-1", days=1)

    assert report.total_cost_usd == Decimal("0.0075")


async def test_repricing_rewrites_history(redis: aioredis.FakeRedis, moment: Clock) -> None:
    """The point of storing tokens: a corrected price corrects every past report."""
    cheap = PriceTable((("gpt-4o", ModelPrice(Decimal("1.00"), Decimal("1.00"))),))
    dear = PriceTable((("gpt-4o", ModelPrice(Decimal("2.00"), Decimal("2.00"))),))
    await SpendLedger(redis, cheap, clock=moment).record(
        "k-1", "gpt-4o", prompt_tokens=1_000_000, completion_tokens=0
    )

    assert (await SpendLedger(redis, cheap, clock=moment).report("k-1", days=1)).total_cost_usd == (
        Decimal("1.00")
    )
    assert (await SpendLedger(redis, dear, clock=moment).report("k-1", days=1)).total_cost_usd == (
        Decimal("2.00")
    )


async def test_an_unpriced_model_is_named_rather_than_costed(
    redis: aioredis.FakeRedis, moment: Clock
) -> None:
    """The total stays honest by admitting what it left out."""
    ledger = SpendLedger(redis, PriceTable(DEFAULT_PRICES), clock=moment)
    await ledger.record("k-1", "gpt-4o", prompt_tokens=1_000_000, completion_tokens=0)
    await ledger.record("k-1", "brand-new-model", prompt_tokens=1_000_000, completion_tokens=0)

    report = await ledger.report("k-1", days=1)

    assert report.unpriced_models == ["brand-new-model"]
    assert report.total_cost_usd == Decimal("2.50")
    assert report.total_tokens == 2_000_000


async def test_a_report_over_a_key_with_no_history_is_empty_not_missing(
    ledger: SpendLedger,
) -> None:
    report = await ledger.report("never-used", days=7)

    assert report.total_requests == 0
    assert report.total_cost_usd is None
    assert len(report.daily) == 7


# --- the requests nobody could measure ---------------------------------------


async def test_an_unmetered_request_is_counted_separately(ledger: SpendLedger) -> None:
    """An abandoned stream cost money and reported no tokens. Calling it free is a lie."""
    await ledger.record("k-1", "gpt-4o", prompt_tokens=100, completion_tokens=100)
    await ledger.record("k-1", "gpt-4o", metered=False)

    report = await ledger.report("k-1", days=1)

    assert report.total_requests == 2
    assert report.total_unmetered_requests == 1
    assert report.total_tokens == 200
    assert report.daily[0].models[0].unmetered_requests == 1


# --- the window --------------------------------------------------------------


async def test_a_report_spans_the_days_asked_for(redis: aioredis.FakeRedis, moment: Clock) -> None:
    """Today counts as one of them: a caller checking their remaining budget
    wants today included, not yesterday's closing figure."""
    ledger = SpendLedger(redis, PriceTable(DEFAULT_PRICES), clock=moment)
    moment.now = TODAY - DAY
    await ledger.record("k-1", "gpt-4o", prompt_tokens=10, completion_tokens=0)
    moment.now = TODAY
    await ledger.record("k-1", "gpt-4o", prompt_tokens=20, completion_tokens=0)

    today_only = await ledger.report("k-1", days=1)
    both = await ledger.report("k-1", days=2)

    assert [day.date for day in today_only.daily] == ["2026-09-06"]
    assert today_only.total_tokens == 20
    assert [day.date for day in both.daily] == ["2026-09-05", "2026-09-06"]
    assert both.total_tokens == 30
    assert (both.start_date, both.end_date) == ("2026-09-05", "2026-09-06")


async def test_a_window_longer_than_retention_is_clamped(
    redis: aioredis.FakeRedis, moment: Clock
) -> None:
    """Asking for a year of a 30-day ledger should not answer with 335 empty days."""
    ledger = SpendLedger(redis, PriceTable(DEFAULT_PRICES), retention_days=7, clock=moment)

    assert len((await ledger.report("k-1", days=365)).daily) == 7


# --- Redis being down --------------------------------------------------------


class BrokenRedis:
    """Fails the way a Redis that has gone away does, at the pipeline."""

    def pipeline(self, transaction: bool = True) -> BrokenRedis:
        return self

    def hincrby(self, *args: object, **kwargs: object) -> None: ...

    def expire(self, *args: object, **kwargs: object) -> None: ...

    def hgetall(self, *args: object, **kwargs: object) -> None: ...

    async def execute(self) -> object:
        raise RedisConnectionError("Error 61 connecting to localhost:6379.")


async def test_a_dead_redis_does_not_fail_the_request() -> None:
    """The provider already answered. Losing the record is bad; losing the answer is worse."""
    ledger = SpendLedger(BrokenRedis(), PriceTable(DEFAULT_PRICES))  # type: ignore[arg-type]

    await ledger.record("k-1", "gpt-4o", prompt_tokens=1, completion_tokens=1)


async def test_a_dead_redis_reports_zeroes_rather_than_erroring() -> None:
    ledger = SpendLedger(BrokenRedis(), PriceTable(DEFAULT_PRICES))  # type: ignore[arg-type]

    report = await ledger.report("k-1", days=3)

    assert report.total_requests == 0
    assert len(report.daily) == 3
