"""Tests for the Redis token-bucket limiter.

Run against `fakeredis[lua]`, which executes the *real* Lua script through lupa
rather than a Python re-implementation of it. That matters more here than
anywhere else in the suite: the property under test is that the check and the
debit happen as one command, and a hand-written double would provide that
property for free and prove nothing.

Time is injected, never slept. A limiter test that waits for a bucket to refill
is a test that takes a minute and still cannot assert an exact number.
"""

import asyncio
from collections.abc import Callable

import pytest
from fakeredis import aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from vortex_ai_gateway.auth import Principal
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest
from vortex_ai_gateway.ratelimit import (
    BucketState,
    Decision,
    RateLimiter,
    _format_reset,
    estimate_tokens,
)

#: A fixed instant, so every assertion about a refill is arithmetic rather than
#: a race with the test's own runtime.
START = 1_700_000_000.0


class Clock:
    """A wall clock the test drives, in seconds."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def redis() -> aioredis.FakeRedis:
    return aioredis.FakeRedis()


@pytest.fixture
def moment() -> Clock:
    return Clock()


@pytest.fixture
def limiter(redis: aioredis.FakeRedis, moment: Clock) -> RateLimiter:
    return RateLimiter(redis, clock=moment)


def caller(**limits: int) -> Principal:
    return Principal(key_id="k-1", **limits)


# --- admission ---------------------------------------------------------------


async def test_a_bucket_admits_exactly_its_capacity(limiter: RateLimiter) -> None:
    principal = caller(rpm=3)

    outcomes = [(await limiter.admit(principal, tokens=1)).allowed for _ in range(5)]

    assert outcomes == [True, True, True, False, False]


async def test_a_bucket_refills_continuously(limiter: RateLimiter, moment: Clock) -> None:
    """Not on the minute: a window that resets admits twice the limit at its edge."""
    principal = caller(rpm=60)
    for _ in range(60):
        await limiter.admit(principal, tokens=1)
    assert (await limiter.admit(principal, tokens=1)).allowed is False

    # 60 rpm is one token a second, so five seconds buys five requests.
    moment.advance(5)

    outcomes = [(await limiter.admit(principal, tokens=1)).allowed for _ in range(6)]
    assert outcomes == [True, True, True, True, True, False]


async def test_a_key_with_no_limits_never_reaches_redis(redis: aioredis.FakeRedis) -> None:
    """What lets a local run work with nothing listening on the Redis port."""
    limiter = RateLimiter(redis)

    decision = await limiter.admit(Principal(key_id="free"), tokens=10_000)

    assert decision.allowed is True
    assert await redis.keys("vortex:rl:*") == []


async def test_only_the_configured_bucket_is_enforced(limiter: RateLimiter) -> None:
    """A limit of zero means unlimited, not "nothing is allowed"."""
    principal = caller(tpm=100)

    admitted = [(await limiter.admit(principal, tokens=40)).allowed for _ in range(4)]

    assert admitted == [True, True, False, False]


# --- the two buckets are one decision ----------------------------------------


async def test_a_token_refusal_does_not_spend_a_request(limiter: RateLimiter) -> None:
    """The whole reason this is a Lua script rather than three round trips."""
    principal = caller(rpm=10, tpm=100)

    refused = await limiter.admit(principal, tokens=1_000)
    assert refused.allowed is False

    # The request bucket must be untouched: nothing was served.
    assert refused.requests.remaining == 10
    allowed = await limiter.admit(principal, tokens=1)
    assert allowed.allowed is True
    assert allowed.requests.remaining == 9


async def test_a_request_refusal_does_not_spend_tokens(limiter: RateLimiter) -> None:
    principal = caller(rpm=1, tpm=10_000)
    await limiter.admit(principal, tokens=500)

    refused = await limiter.admit(principal, tokens=500)

    assert refused.allowed is False
    assert refused.tokens.remaining == 9_500


# --- what the caller is told -------------------------------------------------


async def test_headers_report_both_allowances_on_success(limiter: RateLimiter) -> None:
    """A client that learns its allowance only by exceeding it can only back off late."""
    decision = await limiter.admit(caller(rpm=60, tpm=120_000), tokens=2_000)

    headers = decision.headers()

    assert headers["x-ratelimit-limit-requests"] == "60"
    assert headers["x-ratelimit-remaining-requests"] == "59"
    assert headers["x-ratelimit-limit-tokens"] == "120000"
    assert headers["x-ratelimit-remaining-tokens"] == "118000"
    assert "retry-after" not in headers


async def test_a_rejection_carries_retry_after(limiter: RateLimiter) -> None:
    principal = caller(rpm=60)
    for _ in range(60):
        await limiter.admit(principal, tokens=1)

    rejected = await limiter.admit(principal, tokens=1)

    # 60 rpm refills one token a second, so one second is the honest answer.
    assert rejected.retry_after_seconds == pytest.approx(1.0, abs=0.01)
    assert rejected.headers()["retry-after"] == "1"


async def test_retry_after_is_never_zero(limiter: RateLimiter) -> None:
    """`Retry-After: 0` invites a client straight back into the same rejection."""
    principal = caller(rpm=100_000)
    for _ in range(2):
        await limiter.admit(principal, tokens=1)
    starved = Decision(
        allowed=False,
        requests=BucketState(limit=10, remaining=0, reset_seconds=0.0),
        tokens=BucketState(limit=-1, remaining=-1, reset_seconds=0.0),
        retry_after_seconds=0.0,
    )

    assert starved.headers()["retry-after"] == "1"


async def test_an_unconfigured_bucket_publishes_no_headers(limiter: RateLimiter) -> None:
    """An unlimited allowance reported as a number is a promise we did not make."""
    headers = (await limiter.admit(caller(rpm=10), tokens=5)).headers()

    assert "x-ratelimit-limit-requests" in headers
    assert "x-ratelimit-limit-tokens" not in headers


@pytest.mark.parametrize(
    ("seconds", "formatted"),
    [(0.0, "0.0s"), (1.5, "1.5s"), (59.9, "59.9s"), (60.0, "1m0s"), (135.0, "2m15s")],
)
def test_reset_is_formatted_as_a_duration(seconds: float, formatted: str) -> None:
    """OpenAI's own headers are Go durations, and clients parse them as such."""
    assert _format_reset(seconds) == formatted


# --- reconciliation ----------------------------------------------------------


async def test_an_over_estimate_is_refunded(limiter: RateLimiter) -> None:
    principal = caller(tpm=1_000)
    await limiter.admit(principal, tokens=800)

    await limiter.reconcile(principal, reserved=800, actual=100)

    assert (await limiter.admit(principal, tokens=1)).tokens.remaining == 899


async def test_an_under_estimate_is_charged(limiter: RateLimiter) -> None:
    """The estimate governs what is in flight; the reconcile makes it true."""
    principal = caller(tpm=1_000)
    await limiter.admit(principal, tokens=100)

    await limiter.reconcile(principal, reserved=100, actual=600)

    assert (await limiter.admit(principal, tokens=1)).tokens.remaining == 399


async def test_a_refund_cannot_overfill_the_bucket(limiter: RateLimiter) -> None:
    """Refunding a wild over-estimate blindly would hand out a bigger allowance."""
    principal = caller(tpm=100)
    await limiter.admit(principal, tokens=10)

    await limiter.reconcile(principal, reserved=10_000, actual=0)

    assert (await limiter.admit(principal, tokens=1)).tokens.remaining == 99


async def test_reconciling_an_exact_estimate_touches_nothing(
    limiter: RateLimiter, redis: aioredis.FakeRedis
) -> None:
    principal = caller(tpm=1_000)
    await limiter.admit(principal, tokens=100)
    before = await redis.hgetall("vortex:rl:k-1:tokens")

    await limiter.reconcile(principal, reserved=100, actual=100)

    assert await redis.hgetall("vortex:rl:k-1:tokens") == before


# --- Redis being down --------------------------------------------------------


class BrokenRedis:
    """Everything this limiter asks of Redis, failing the way a dead one does."""

    def register_script(self, script: str) -> Callable[..., object]:
        async def run(**kwargs: object) -> object:
            raise RedisConnectionError("Error 61 connecting to localhost:6379.")

        return run


async def test_a_dead_redis_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """A limiter that takes the gateway down when it cannot enforce a limit
    has inverted its own purpose."""
    limiter = RateLimiter(BrokenRedis())  # type: ignore[arg-type]

    decision = await limiter.admit(caller(rpm=1, tpm=1), tokens=10_000)

    assert decision.allowed is True
    assert decision.headers() == {}


async def test_a_dead_redis_does_not_break_reconciliation() -> None:
    limiter = RateLimiter(BrokenRedis())  # type: ignore[arg-type]

    await limiter.reconcile(caller(tpm=100), reserved=100, actual=1)


# --- the estimate ------------------------------------------------------------


def request_for(text: str, **extra: object) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": text}], **extra}
    )


def test_the_estimate_measures_the_prompt_and_assumes_the_completion() -> None:
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=100)

    estimate = estimate_tokens(request_for("a" * 40), settings)

    assert estimate == 40 // 4 + 100


def test_a_declared_max_beats_the_assumption() -> None:
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=100)

    estimate = estimate_tokens(request_for("a" * 40, max_completion_tokens=7), settings)

    assert estimate == 10 + 7


def test_asking_for_several_choices_multiplies_the_completion() -> None:
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=50)

    estimate = estimate_tokens(request_for("a" * 8, n=4), settings)

    assert estimate == 2 + 50 * 4


def test_multimodal_content_counts_its_text_parts() -> None:
    """An image's tokens are unknowable here; the reconcile is what corrects for it."""
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=0)
    request = ChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "b" * 20},
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                    ],
                }
            ],
        }
    )

    assert estimate_tokens(request, settings) == 5


def test_an_empty_request_still_reserves_something() -> None:
    """Zero would let an unbounded number of requests through the token bucket."""
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=0)

    assert estimate_tokens(request_for(""), settings) == 1


# --- the property the Lua exists for -----------------------------------------


async def test_two_hundred_concurrent_requests_never_over_admit(
    limiter: RateLimiter, redis: aioredis.FakeRedis
) -> None:
    """The experiment from the day-07 design note.

    Fire far more requests than the bucket holds, all at once, against a Redis
    running the real script. Any admission past the fiftieth means the check and
    the debit were separable and something interleaved between them — the exact
    bug a sequential test cannot see, because sequentially there is nothing to
    interleave with.
    """
    principal = caller(rpm=50, tpm=50_000)

    decisions = await asyncio.gather(*(limiter.admit(principal, tokens=1) for _ in range(200)))

    admitted = [decision for decision in decisions if decision.allowed]
    assert len(admitted) == 50
    assert len(decisions) - len(admitted) == 150
    assert all(decision.headers()["retry-after"] for decision in decisions if not decision.allowed)
    # And the bucket agrees with the count of admissions, rather than merely
    # having stopped somewhere near it.
    assert float((await redis.hget("vortex:rl:k-1:requests", "tokens")) or 1) == pytest.approx(0.0)


async def test_concurrent_token_spend_never_over_admits(limiter: RateLimiter) -> None:
    """The same property for the bucket whose costs are not all 1."""
    principal = caller(tpm=1_000)

    decisions = await asyncio.gather(*(limiter.admit(principal, tokens=100) for _ in range(50)))

    assert sum(decision.allowed for decision in decisions) == 10


def test_a_message_with_no_content_contributes_nothing() -> None:
    """An assistant turn that was only a tool call has no text to measure."""
    settings = Settings(_env_file=None, rate_limit_assumed_completion_tokens=3)
    request = ChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "user", "content": "abcd"},
            ],
        }
    )

    assert estimate_tokens(request, settings) == 1 + 3
