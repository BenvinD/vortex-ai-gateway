"""The retry loop, the breaker, and the fallback chain.

These are behaviour tests, not timing tests: ``asyncio.sleep`` is captured
rather than served, so a test asserting on a five-minute backoff still runs in
microseconds and never flakes on a loaded machine. The one thing the clock is
used for is the breaker's open window, and that is driven by a fake clock for
the same reason.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from tests.upstream import chat_request
from vortex_ai_gateway import resilience
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from vortex_ai_gateway.providers.anthropic import AnthropicAdapter
from vortex_ai_gateway.providers.errors import (
    CircuitOpenError,
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderUnavailable,
)
from vortex_ai_gateway.providers.mock import MockProvider
from vortex_ai_gateway.providers.resilience_wrapper import (
    FallbackProvider,
    ResilientProvider,
)
from vortex_ai_gateway.resilience import CircuitBreaker, RetryBudget, RetryPolicy
from vortex_ai_gateway.routing import RoutingConfigError, build_router, parse_fallback_chains


class ScriptedProvider:
    """A provider that replays a script of outcomes and counts its calls.

    An entry that is an exception is raised; anything else is served by a real
    :class:`MockProvider`, so a "success" is a genuinely well-formed response
    rather than a sentinel the assertions have to special-case.
    """

    def __init__(self, *script: Exception | None, name: str = "primary") -> None:
        self.name = name
        self._script: list[Exception | None] = list(script)
        self._inner = MockProvider(name=name)
        self.calls = 0

    def _next(self) -> Exception | None:
        outcome = self._script[min(self.calls, len(self._script) - 1)] if self._script else None
        self.calls += 1
        return outcome

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        outcome = self._next()
        if isinstance(outcome, Exception):
            raise outcome
        return await self._inner.complete(request)

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        return self._inner.stream(request)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every backoff instead of serving it, and record the durations."""
    recorded: list[float] = []

    async def _capture(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _capture)
    return recorded


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """A fake monotonic clock the tests advance by hand."""
    current = 0.0

    def advance(seconds: float) -> None:
        nonlocal current
        current += seconds

    monkeypatch.setattr(resilience, "now", lambda: current)
    return advance


def policy(**overrides: object) -> RetryPolicy:
    """A retry policy with fast, deterministic defaults."""
    settings: dict[str, object] = {"max_attempts": 3, "backoff_base": 1.0, "max_backoff": 8.0}
    settings.update(overrides)
    return RetryPolicy(**settings)  # type: ignore[arg-type]


def resilient(inner: ScriptedProvider, **overrides: object) -> ResilientProvider:
    """``inner`` wrapped with a retry policy and a breaker of its own."""
    breaker = CircuitBreaker(name=inner.name, failure_threshold=2, open_seconds=30.0)
    return ResilientProvider(inner=inner, policy=policy(**overrides), breaker=breaker)


# --- the four scenarios ----------------------------------------------------


async def test_transient_500s_are_retried_until_one_succeeds(sleeps: list[float]) -> None:
    """A 5xx is retryable, so the third attempt gets to answer."""
    inner = ScriptedProvider(
        ProviderUnavailable("upstream 500", provider="primary", status_code=500),
        ProviderUnavailable("upstream 500", provider="primary", status_code=500),
        None,
    )
    wrapper = resilient(inner)

    completion = await wrapper.complete(chat_request())

    assert inner.calls == 3
    assert completion.choices[0].message.content
    assert len(sleeps) == 2, "one backoff between each pair of attempts"
    # A success clears the breaker: two transient failures are not an outage.
    assert await wrapper.breaker.state() is resilience.BreakerState.CLOSED


async def test_a_429_waits_exactly_as_long_as_the_provider_asked(sleeps: list[float]) -> None:
    """``Retry-After`` beats our own backoff, which would be throttled again."""
    inner = ScriptedProvider(
        ProviderRateLimited("slow down", provider="primary", status_code=429, retry_after=7.5),
        None,
    )
    wrapper = resilient(inner)

    await wrapper.complete(chat_request())

    assert inner.calls == 2
    assert sleeps == [7.5], "the provider's number is used verbatim, not jittered backoff"


async def test_a_400_is_not_retried_and_does_not_move_the_breaker(sleeps: list[float]) -> None:
    """The caller's own bad request would fail identically on every attempt."""
    inner = ScriptedProvider(ProviderBadRequest("unknown model", provider="primary"))
    wrapper = resilient(inner)

    with pytest.raises(ProviderBadRequest):
        await wrapper.complete(chat_request())

    assert inner.calls == 1, "no retry"
    assert sleeps == [], "no backoff"
    # The important half: one client sending garbage must not open the breaker
    # and take the provider offline for everyone else.
    for _ in range(5):
        with pytest.raises(ProviderBadRequest):
            await wrapper.complete(chat_request())
    assert await wrapper.breaker.state() is resilience.BreakerState.CLOSED


async def test_a_dead_provider_opens_its_breaker_and_the_chain_falls_back(
    sleeps: list[float],
) -> None:
    """The breaker trips, and the next provider in the chain serves the call."""
    down = ScriptedProvider(
        ProviderUnavailable("connection refused", provider="primary"), name="primary"
    )
    healthy = ScriptedProvider(name="secondary")
    chain = FallbackProvider([resilient(down), resilient(healthy)])

    # Threshold is two failed *requests*, so the first two hop after exhausting
    # their retries and the breaker opens.
    first = await chain.complete(chat_request())
    second = await chain.complete(chat_request())
    assert first.vortex is not None and first.vortex.provider == "secondary"
    assert second.vortex is not None and second.vortex.provider == "secondary"
    assert down.calls == 6, "3 attempts per request while the breaker was closed"

    breaker = chain.providers[0].breaker  # type: ignore[union-attr]
    assert await breaker.state() is resilience.BreakerState.OPEN

    # From here the dead provider is not called at all: the hop is instant.
    third = await chain.complete(chat_request())
    assert third.vortex is not None and third.vortex.provider == "secondary"
    assert down.calls == 6, "an open breaker means no upstream call is made"


# --- the retry loop's other exits ------------------------------------------


async def test_the_retry_budget_stops_a_second_request_from_retrying(
    sleeps: list[float],
) -> None:
    """Attempts bound one request; the budget bounds retries in aggregate."""
    inner = ScriptedProvider(ProviderUnavailable("down", provider="primary"))
    budget = RetryBudget(capacity=2, refill_per_second=0.0)
    wrapper = resilient(inner, budget=budget)

    with pytest.raises(ProviderUnavailable):
        await wrapper.complete(chat_request())
    assert inner.calls == 3, "two retries, both paid for out of the budget"

    inner.calls = 0
    with pytest.raises(ProviderUnavailable):
        await wrapper.complete(chat_request())
    assert inner.calls == 1, "budget empty: the first failure is now final"


async def test_a_budget_refills_over_time(clock: Callable[[float], None]) -> None:
    budget = RetryBudget(capacity=1, refill_per_second=1.0)
    assert await budget.consume() is True
    assert await budget.consume() is False
    clock(1.0)
    assert await budget.consume() is True


async def test_the_deadline_stops_a_retry_that_would_overrun_it(
    sleeps: list[float], clock: Callable[[float], None]
) -> None:
    """A sleep that lands past the deadline is not worth a worker or a wait."""
    inner = ScriptedProvider(
        ProviderRateLimited("slow down", provider="primary", retry_after=30.0), None
    )
    wrapper = resilient(inner, deadline_seconds=10.0)

    with pytest.raises(ProviderRateLimited):
        await wrapper.complete(chat_request())

    assert inner.calls == 1
    assert sleeps == [], "gave up rather than sleeping into a deadline it would miss"


async def test_the_last_attempt_does_not_pay_for_a_backoff(sleeps: list[float]) -> None:
    inner = ScriptedProvider(ProviderUnavailable("down", provider="primary"))
    wrapper = resilient(inner, max_attempts=3)

    with pytest.raises(ProviderUnavailable):
        await wrapper.complete(chat_request())

    assert inner.calls == 3
    assert len(sleeps) == 2, "backoff only between attempts, never after the last"


async def test_an_open_breaker_reports_when_to_come_back() -> None:
    """The refusal carries the number ``routes.py`` turns into Retry-After."""
    inner = ScriptedProvider(ProviderUnavailable("down", provider="primary"))
    wrapper = resilient(inner, max_attempts=1)

    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            await wrapper.complete(chat_request())

    with pytest.raises(CircuitOpenError) as caught:
        await wrapper.complete(chat_request())
    assert caught.value.retry_after is not None
    assert 0 < caught.value.retry_after <= 30.0
    assert caught.value.retryable is False, "retrying inside the open window is the one sin"


# --- the breaker's own state machine ---------------------------------------


async def test_the_breaker_probes_with_exactly_one_trial(clock: Callable[[float], None]) -> None:
    breaker = CircuitBreaker(name="primary", failure_threshold=1, open_seconds=10.0)
    await breaker.record_failure()
    assert await breaker.state() is resilience.BreakerState.OPEN
    assert await breaker.allow() is False

    clock(10.0)
    assert await breaker.allow() is True, "the first caller takes the trial"
    assert await breaker.allow() is False, "everyone else still waits"
    assert await breaker.state() is resilience.BreakerState.HALF_OPEN

    await breaker.record_success()
    assert await breaker.state() is resilience.BreakerState.CLOSED
    assert await breaker.allow() is True


async def test_a_failed_trial_reopens_the_breaker_for_longer(
    clock: Callable[[float], None],
) -> None:
    """Windows grow by the multiplier once per reopen, not twice."""
    breaker = CircuitBreaker(
        name="primary", failure_threshold=1, open_seconds=10.0, backoff_multiplier=2.0
    )
    windows: list[float] = []
    for _ in range(3):
        await breaker.record_failure()
        remaining = await breaker.retry_after_seconds()
        assert remaining is not None
        windows.append(remaining)
        clock(remaining)
        await breaker.allow()  # take the trial, which then fails on the next loop

    assert windows == [10.0, 20.0, 40.0]


async def test_the_open_window_stops_growing_at_the_cap(clock: Callable[[float], None]) -> None:
    breaker = CircuitBreaker(
        name="primary",
        failure_threshold=1,
        open_seconds=10.0,
        backoff_multiplier=10.0,
        max_open_seconds=50.0,
    )
    windows: list[float] = []
    for _ in range(4):
        await breaker.record_failure()
        remaining = await breaker.retry_after_seconds()
        assert remaining is not None
        windows.append(remaining)
        clock(remaining)
        await breaker.allow()  # take the trial, which fails on the next loop

    assert windows == [10.0, 50.0, 50.0, 50.0], "x10 would be 100s; the cap holds it at 50"


async def test_a_cancelled_trial_hands_the_slot_back(clock: Callable[[float], None]) -> None:
    """A client disconnect must not wedge the breaker in half-open forever."""

    class Hangs:
        name = "primary"

        async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
            raise asyncio.CancelledError

        def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
            raise NotImplementedError

    breaker = CircuitBreaker(name="primary", failure_threshold=1, open_seconds=10.0)
    wrapper = ResilientProvider(inner=Hangs(), policy=policy(), breaker=breaker)
    await breaker.record_failure()
    clock(10.0)

    with pytest.raises(asyncio.CancelledError):
        await wrapper.complete(chat_request())

    assert await breaker.allow() is True, "the trial slot was released, not leaked"


async def test_state_is_a_pure_read(clock: Callable[[float], None]) -> None:
    breaker = CircuitBreaker(name="primary", failure_threshold=1, open_seconds=10.0)
    await breaker.record_failure()
    clock(10.0)
    assert await breaker.state() is resilience.BreakerState.OPEN
    assert await breaker.state() is resilience.BreakerState.OPEN, "reading changed nothing"
    assert await breaker.allow() is True, "only allow() takes the trial"


# --- the fallback chain -----------------------------------------------------


async def test_a_bad_request_is_never_hopped_to_the_next_provider() -> None:
    """The next provider would reject it too, for a second provider's money."""
    first = ScriptedProvider(ProviderBadRequest("unknown model", provider="primary"))
    second = ScriptedProvider(name="secondary")
    chain = FallbackProvider([resilient(first), resilient(second)])

    with pytest.raises(ProviderBadRequest):
        await chain.complete(chat_request())

    assert second.calls == 0


async def test_a_chain_that_runs_out_reports_the_last_failure(sleeps: list[float]) -> None:
    first = ScriptedProvider(ProviderUnavailable("down", provider="primary"), name="primary")
    second = ScriptedProvider(
        ProviderUnavailable("also down", provider="secondary"), name="secondary"
    )
    chain = FallbackProvider([resilient(first), resilient(second)])

    with pytest.raises(ProviderUnavailable) as caught:
        await chain.complete(chat_request())

    assert caught.value.provider == "secondary", "the error the caller sees is the last hop's"


async def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackProvider([])


def test_a_wrapper_names_the_provider_it_holds() -> None:
    """Logs and ``response.vortex`` must keep naming the real adapter."""
    chain = FallbackProvider([MockProvider(name="primary"), MockProvider(name="secondary")])
    assert chain.name == "primary"
    assert "primary > secondary" in repr(chain)
    wrapped = ResilientProvider(
        inner=MockProvider(name="primary"), policy=policy(), breaker=CircuitBreaker(name="primary")
    )
    assert wrapped.name == "primary"
    assert "MockProvider" in repr(wrapped)


# --- streaming and teardown -------------------------------------------------


async def test_a_stream_is_forwarded_untouched_by_both_wrappers() -> None:
    """Retrying a committed stream would splice two generations into one body."""
    wrapper = resilient(ScriptedProvider())
    chunks = [chunk async for chunk in wrapper.stream(chat_request(stream=True))]
    assert chunks and chunks[-1].choices[0].finish_reason == "stop"

    chain = FallbackProvider([MockProvider(name="primary"), MockProvider(name="secondary")])
    from_chain = [chunk async for chunk in chain.stream(chat_request(stream=True))]
    assert from_chain[0].vortex is not None
    assert from_chain[0].vortex.provider == "primary", "the head of the chain streams"


async def test_closing_reaches_every_adapter_exactly_once() -> None:
    class Closes:
        def __init__(self, name: str) -> None:
            self.name = name
            self.closed = 0

        async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
            raise NotImplementedError

        def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
            raise NotImplementedError

        async def aclose(self) -> None:
            self.closed += 1

    first, second = Closes("primary"), Closes("secondary")
    breaker = CircuitBreaker(name="primary")
    chain = FallbackProvider(
        [
            ResilientProvider(inner=first, policy=policy(), breaker=breaker),
            ResilientProvider(inner=second, policy=policy(), breaker=breaker),
        ]
    )
    await chain.aclose()
    assert (first.closed, second.closed) == (1, 1)


# --- wiring from configuration ---------------------------------------------


def test_a_policy_is_built_from_settings() -> None:
    from vortex_ai_gateway.config import Settings
    from vortex_ai_gateway.providers.resilience_wrapper import build_policy, wrap_with_resilience

    settings = Settings(
        retry_max_attempts=5,
        retry_backoff_seconds=0.25,
        retry_deadline_seconds=45.0,
        retry_budget_capacity=8,
        retry_budget_refill_per_second=2.0,
        breaker_failure_threshold=7,
    )
    built = build_policy(settings)
    assert built.max_attempts == 5
    assert built.deadline_seconds == 45.0
    assert built.budget is not None and built.budget.capacity == 8

    wrapped = wrap_with_resilience(MockProvider(name="openai"), settings)
    assert wrapped.name == "openai", "the wrapper answers to the adapter it holds"
    assert wrapped.breaker.name == "openai", "each provider gets a breaker of its own"
    assert wrapped.breaker.failure_threshold == 7


def test_a_budget_is_off_unless_both_of_its_numbers_are_set() -> None:
    from vortex_ai_gateway.config import Settings
    from vortex_ai_gateway.providers.resilience_wrapper import build_policy

    assert build_policy(Settings()).budget is None
    assert build_policy(Settings(retry_budget_capacity=5)).budget is None
    assert build_policy(Settings(retry_budget_refill_per_second=1.0)).budget is None


# --- fallback chains from the environment ----------------------------------


def test_fallback_chains_parse_in_order() -> None:
    assert parse_fallback_chains("openai>anthropic>ollama") == {
        "openai": ("openai", "anthropic", "ollama")
    }
    assert parse_fallback_chains(" openai > anthropic , anthropic > ollama ") == {
        "openai": ("openai", "anthropic"),
        "anthropic": ("anthropic", "ollama"),
    }
    assert parse_fallback_chains("") == {}


@pytest.mark.parametrize(
    ("spec", "complaint"),
    [
        ("openai", "not 'primary>next"),
        ("openai>", "not 'primary>next"),
        ("openai>anthropic>openai", "names a provider twice"),
        ("openai>anthropic,openai>ollama", "heads more than one"),
    ],
)
def test_a_malformed_chain_is_refused_at_boot(spec: str, complaint: str) -> None:
    """A chain that cannot work must not wait for the first request to say so."""
    with pytest.raises(RoutingConfigError, match=complaint):
        parse_fallback_chains(spec)


def test_a_chain_head_nothing_routes_to_is_refused() -> None:
    with pytest.raises(RoutingConfigError, match="no routing rule sends traffic to"):
        build_router(
            Settings(
                model_routes="gpt-*=openai",
                fallback_chains="anthropic>ollama",
                openai_api_key="k",
                anthropic_api_key="k",
            )
        )


def test_a_fallback_member_needs_its_own_key() -> None:
    """A chain member is built like any other provider, so it is key-checked."""
    with pytest.raises(RoutingConfigError, match="VORTEX_ANTHROPIC_API_KEY"):
        build_router(
            Settings(
                model_routes="gpt-*=openai",
                fallback_chains="openai>anthropic",
                openai_api_key="k",
            )
        )


def test_the_router_hands_out_a_chain_where_one_is_configured() -> None:
    router = build_router(
        Settings(
            model_routes="gpt-*=openai,claude-*=anthropic",
            fallback_chains="openai>anthropic",
            openai_api_key="k",
            anthropic_api_key="k",
        )
    )
    assert router is not None
    served = router.provider_for("gpt-4o")
    assert isinstance(served, FallbackProvider)
    assert [p.name for p in served.providers] == ["openai", "anthropic"]

    # A provider with no chain is still wrapped, just not composed.
    direct = router.provider_for("claude-sonnet-4")
    assert isinstance(direct, ResilientProvider)
    assert isinstance(direct.inner, AnthropicAdapter)


def test_every_provider_gets_its_own_breaker() -> None:
    """A dead OpenAI must not open Anthropic's circuit."""
    router = build_router(
        Settings(
            model_routes="gpt-*=openai,claude-*=anthropic",
            openai_api_key="k",
            anthropic_api_key="k",
        )
    )
    assert router is not None
    breakers = {name: provider.breaker for name, provider in router.providers.items()}  # type: ignore[union-attr]
    assert breakers["openai"] is not breakers["anthropic"]
    assert {b.name for b in breakers.values()} == {"openai", "anthropic"}


def test_a_breaker_says_which_provider_it_guards() -> None:
    """Breakers are per-provider, so a log line or a repr has to name one."""
    assert "openai" in repr(CircuitBreaker(name="openai"))


async def test_a_closed_breaker_has_no_retry_after() -> None:
    assert await CircuitBreaker(name="openai").retry_after_seconds() is None
