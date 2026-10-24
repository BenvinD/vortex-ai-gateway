"""Providers that add resilience by composing other providers.

Both classes here are :class:`~vortex_ai_gateway.providers.base.ChatProvider`
implementations that hold other providers, which is the same seam
:class:`~vortex_ai_gateway.routing.ProviderRouter` uses — resilience composes
*through* the interface rather than sitting beside it.

* :class:`ResilientProvider` wraps exactly one adapter with a retry loop and
  that provider's own circuit breaker. One breaker per adapter is the whole
  point: a dead OpenAI must not stop Anthropic traffic (ADR-002).
* :class:`FallbackProvider` tries a chain of providers in order, moving on only
  when a failure means *this provider* cannot serve the request — never when it
  means nobody can (ADR-020).

A stream gets the same retries, breaker and hops as a completion, but only up
to its first chunk (ADR-032). Until then nothing has been sent, and the route
holds the ``200`` until it has that chunk in hand; after it, a retry or a hop
would splice two generations into one response, so the rest is relayed
untouched. Both classes run one loop for both shapes, generic over what a
single attempt is: awaiting a completion, or opening a stream and awaiting its
first chunk.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Final

import structlog

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from vortex_ai_gateway.metrics import (
    FALLBACKS,
    PROVIDER_ATTEMPTS,
    PROVIDER_GIVE_UPS,
    PROVIDER_RETRIES,
)
from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.errors import CircuitOpenError, ProviderError
from vortex_ai_gateway.resilience import CircuitBreaker, RetryBudget, RetryPolicy, now
from vortex_ai_gateway.streaming import aclose_stream
from vortex_ai_gateway.tracing import span

logger = structlog.get_logger(__name__)

#: One event name per kind, so "how often did we retry?" and "what fell back?"
#: are each a single log filter (the pattern `streaming.STREAM_EVENT` uses).
RETRY_EVENT: Final = "provider call retried"
GIVE_UP_EVENT: Final = "provider call failed after retries"
FALLBACK_EVENT: Final = "provider fallback"

#: Why a retry sequence stopped, as the ``reason`` label on
#: ``vortex_provider_give_ups_total``. A closed set, so the four ways to give
#: up stay distinguishable on a graph: running out of attempts is a provider
#: problem, running out of budget is a *fleet* problem and means the outage is
#: wider than this request, running out of deadline means the retries were
#: never going to finish in time and the policy is mistuned (ADR-001), and a
#: failed trial is the breaker's probe finding the provider still down — one
#: call by design, not a sequence cut short (ADR-030).
GAVE_UP_EXHAUSTED: Final = "attempts exhausted"
GAVE_UP_BUDGET: Final = "retry budget exhausted"
GAVE_UP_DEADLINE: Final = "deadline reached"
GAVE_UP_TRIAL: Final = "half-open trial failed"


def _retry_after_of(exc: ProviderError) -> float | None:
    """The wait a provider asked for, when it named one.

    Only rate limits and an open breaker carry it, and both are typed, but a
    duck-typed read keeps this from having to import every error that might
    grow the attribute later.
    """
    value = getattr(exc, "retry_after", None)
    return float(value) if isinstance(value, int | float) else None


#: A stream that has started, and the first chunk it produced — ``None`` for a
#: stream that ended without producing any.
type Opened = tuple[AsyncIterator[ChatCompletionChunk], ChatCompletionChunk | None]


async def _open(stream: AsyncIterator[ChatCompletionChunk]) -> Opened:
    """Await ``stream``'s first chunk: one attempt, for a stream (ADR-032).

    An adapter's stream makes its HTTP request when it is first iterated, so
    this is where a refused key, an unknown model, a ``429`` or a ``5xx``
    surfaces — while nothing has been sent, and the failure can still be
    retried, hopped or turned into a status code. A stream that fails here is
    closed before the failure propagates, so a retry never leaves the attempt
    before it holding a connection.
    """
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        return stream, None
    except BaseException:
        await aclose_stream(stream)
        raise
    return stream, first


class ResilientProvider:
    """One adapter, wrapped in a retry loop and its own circuit breaker."""

    def __init__(
        self,
        inner: ChatProvider,
        policy: RetryPolicy,
        breaker: CircuitBreaker,
    ) -> None:
        self.inner = inner
        self.policy = policy
        self.breaker = breaker
        #: The wrapper reports the wrapped provider's name, so logs and
        #: ``response.vortex`` keep naming the adapter that actually served.
        self.name = inner.name

    def __repr__(self) -> str:
        return f"ResilientProvider({self.inner!r})"

    async def _refuse(self) -> CircuitOpenError:
        """The error raised while the breaker is open, carrying its own clock."""
        retry_after = await self.breaker.retry_after_seconds()
        return CircuitOpenError(
            f"The {self.name} provider is not being called: its circuit breaker is open.",
            provider=self.name,
            retry_after=retry_after,
        )

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Serve ``request``, retrying transient failures within the deadline."""
        return await self._call(request, lambda: self.inner.complete(request))

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Stream ``request``, retried as :meth:`complete` is until its first chunk.

        The first chunk is the breaker's verdict: it arriving is a success, and
        a failure after it moves nothing, because the upstream has demonstrably
        answered. The rest is relayed untouched, and the inner stream is closed
        in a ``finally`` so a client hanging up still reaches the adapter and
        closes the upstream connection (ADR-019).
        """
        stream, first = await self._call(request, lambda: _open(self.inner.stream(request)))
        try:
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk
        finally:
            await aclose_stream(stream)

    async def _call[T](
        self, request: ChatCompletionRequest, attempt_once: Callable[[], Awaitable[T]]
    ) -> T:
        """Run ``attempt_once`` under the retry policy and this provider's breaker.

        One loop for both shapes of request, so the rules it took ADR-030 to
        get right — the permit, the one-call trial, the slot handed back in a
        ``finally``, the single give-up report — cannot drift between them.
        """
        started = now()
        deadline = self.policy.deadline_seconds
        last_exc: ProviderError | None = None
        gave_up: str | None = None

        for attempt in range(self.policy.max_attempts):
            # Consulted before *every* attempt, not once per request: when the
            # breaker opens mid-sequence the remaining attempts must stop.
            permit = await self.breaker.allow()
            if permit is None:
                raise await self._refuse()

            try:
                # One span per attempt, not per request: three spans under one
                # `provider.complete` parent is what a retry storm looks like,
                # and it is the picture Day 5's log lines were describing.
                with span(
                    "provider.attempt",
                    {
                        "vortex.provider": self.name,
                        "vortex.attempt": attempt + 1,
                        "vortex.max_attempts": self.policy.max_attempts,
                    },
                ):
                    result = await attempt_once()
            except asyncio.CancelledError:
                # The caller went away. Nothing was learned about the upstream;
                # the `finally` below hands back a trial slot if this held one.
                PROVIDER_ATTEMPTS.labels(provider=self.name, outcome="cancelled").inc()
                raise
            except ProviderError as exc:
                # Counted before the retryable check, because the point of this
                # counter is how many times the upstream was *called*: divided
                # into `vortex_requests_total` it is the amplification factor,
                # and a non-retryable failure still cost one call.
                PROVIDER_ATTEMPTS.labels(provider=self.name, outcome="error").inc()
                if not exc.retryable:
                    # The caller's own bad request, our bad key, a body we
                    # cannot read. None of these say the provider is unhealthy,
                    # so none of them may move the breaker.
                    raise
                if permit.trial:
                    # The half-open trial is one call, never a retry sequence
                    # (ADR-030): its own next `allow()` would be refused by its
                    # own reservation, and sleeping through a backoff on the
                    # only slot refuses every other caller too. Reopen now.
                    await self.breaker.record_failure()
                    self._report_give_up(request, exc, GAVE_UP_TRIAL, attempts=attempt + 1)
                    raise
                last_exc = exc
                gave_up = await self._backoff_or_give_up(exc, attempt, started, deadline)
                if gave_up is not None:
                    break
            else:
                PROVIDER_ATTEMPTS.labels(provider=self.name, outcome="ok").inc()
                await self.breaker.record_success()
                return result
            finally:
                # Every exit above that recorded no verdict — a non-retryable
                # failure, a cancellation, an exception that is not a
                # `ProviderError` — must hand a trial slot back, or the breaker
                # sits in HALF_OPEN refusing everyone forever (ADR-030). It
                # releases nothing for any other permit, or after a verdict.
                await self.breaker.release_trial(permit)

        await self.breaker.record_failure()
        if last_exc is None or gave_up is None:  # pragma: no cover - the loop sets both
            raise CircuitOpenError(f"{self.name} exhausted its retries", provider=self.name)
        self._report_give_up(request, last_exc, gave_up, attempts=attempt + 1)
        raise last_exc

    def _report_give_up(
        self, request: ChatCompletionRequest, exc: ProviderError, reason: str, *, attempts: int
    ) -> None:
        """Write the one log line and the one count a request that gave up gets.

        One place, so one give-up is one event: the line used to be written
        here *and* by whatever decided to stop, and the counter incremented
        under both reasons, so a budget give-up read as two on the dashboard.
        """
        logger.warning(
            GIVE_UP_EVENT,
            provider=self.name,
            model=request.model,
            reason=reason,
            attempts=attempts,
            error=type(exc).__name__,
            code=exc.code,
        )
        PROVIDER_GIVE_UPS.labels(provider=self.name, reason=reason).inc()

    async def _backoff_or_give_up(
        self,
        exc: ProviderError,
        attempt: int,
        started: float,
        deadline: float | None,
    ) -> str | None:
        """Sleep out the backoff before another attempt, or say why there is none.

        Returns ``None`` once it has waited, or the give-up reason without
        reporting it: deciding and reporting are separate so that only
        :meth:`_call` reports, exactly once.
        """
        if attempt >= self.policy.max_attempts - 1:
            return GAVE_UP_EXHAUSTED

        # A budget protects the provider in aggregate; per-request attempt
        # counts cannot. Consumed only for retries, never the first call.
        if self.policy.budget is not None and not await self.policy.budget.consume():
            return GAVE_UP_BUDGET

        # The provider's own Retry-After beats our arithmetic: a policy that
        # substitutes its own backoff gets throttled again.
        retry_after = _retry_after_of(exc)
        honoured = retry_after is not None
        sleep_for = (
            retry_after if retry_after is not None else self.policy.backoff_with_jitter(attempt)
        )

        if deadline is not None:
            remaining = deadline - (now() - started)
            if remaining <= 0 or sleep_for > remaining:
                # Sleeping into a deadline we will miss wastes a worker and the
                # client's patience both. Give up now and report the failure.
                return GAVE_UP_DEADLINE

        logger.info(
            RETRY_EVENT,
            provider=self.name,
            attempt=attempt + 1,
            of=self.policy.max_attempts,
            sleep_s=round(sleep_for, 3),
            retry_after_honoured=honoured,
            error=type(exc).__name__,
            code=exc.code,
            status=exc.status_code,
        )
        # Counted here and not at the top of the function: everything above
        # this line is a reason *not* to retry, so a counter set earlier would
        # report retries that never happened.
        PROVIDER_RETRIES.labels(provider=self.name, error=type(exc).__name__).inc()
        await asyncio.sleep(sleep_for)
        return None

    async def aclose(self) -> None:
        """Close the wrapped provider, if it owns anything."""
        closer = getattr(self.inner, "aclose", None)
        if closer is not None:
            await closer()


class FallbackProvider:
    """Try each provider in a chain until one serves the request.

    A hop happens only for failures that mean *this* provider cannot serve the
    call right now — its breaker is open, or it exhausted its retries on a
    transient fault. A :class:`~vortex_ai_gateway.providers.errors.ProviderBadRequest`
    is never hopped: the request is malformed, and the next provider would
    reject it too, so failing over would just spend a second provider's quota
    to return the same 400 more slowly.
    """

    def __init__(self, providers: Sequence[ChatProvider], name: str | None = None) -> None:
        if not providers:
            raise ValueError("a fallback chain needs at least one provider")
        self.providers = tuple(providers)
        #: Named for the head of the chain, so ``response.vortex`` and the logs
        #: still identify the provider that actually answered.
        self.name = name or providers[0].name

    def __repr__(self) -> str:
        return f"FallbackProvider({' > '.join(p.name for p in self.providers)})"

    @staticmethod
    def _is_hoppable(exc: ProviderError) -> bool:
        """Whether another provider deserves a try at this failure."""
        return isinstance(exc, CircuitOpenError) or exc.retryable

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Serve ``request`` from the first provider in the chain that can."""
        return await self._first_to_serve(request, lambda provider: provider.complete(request))

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Stream from the first provider in the chain that produces a chunk.

        A hop happens only before that chunk (ADR-032). After it, the ``200``
        is committed and a hop would splice two providers' generations into one
        body, so a mid-stream failure is the caller's, as an event.
        """
        stream, first = await self._first_to_serve(
            request, lambda provider: _open(provider.stream(request))
        )
        try:
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk
        finally:
            await aclose_stream(stream)

    async def _first_to_serve[T](
        self, request: ChatCompletionRequest, attempt: Callable[[ChatProvider], Awaitable[T]]
    ) -> T:
        """Run ``attempt`` against each provider in turn until one succeeds."""
        last_exc: ProviderError | None = None
        for position, provider in enumerate(self.providers):
            try:
                return await attempt(provider)
            except ProviderError as exc:
                if not self._is_hoppable(exc):
                    raise
                last_exc = exc
                remaining = self.providers[position + 1 :]
                if not remaining:
                    break
                logger.warning(
                    FALLBACK_EVENT,
                    model=request.model,
                    from_provider=provider.name,
                    to_provider=remaining[0].name,
                    reason=type(exc).__name__,
                    code=exc.code,
                    chain_position=position + 1,
                )
                FALLBACKS.labels(from_provider=provider.name, to_provider=remaining[0].name).inc()

        if last_exc is None:  # pragma: no cover - the loop cannot end without one
            raise ValueError("a fallback chain needs at least one provider")
        raise last_exc

    async def aclose(self) -> None:
        """Close every provider in the chain."""
        for provider in self.providers:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


def build_policy(settings: Settings) -> RetryPolicy:
    """The retry policy described by ``settings``."""
    budget: RetryBudget | None = None
    if settings.retry_budget_capacity > 0 and settings.retry_budget_refill_per_second > 0:
        budget = RetryBudget(
            capacity=settings.retry_budget_capacity,
            refill_per_second=settings.retry_budget_refill_per_second,
        )
    return RetryPolicy(
        max_attempts=settings.retry_max_attempts,
        backoff_base=settings.retry_backoff_seconds,
        max_backoff=settings.retry_max_backoff_seconds,
        deadline_seconds=settings.retry_deadline_seconds,
        budget=budget,
    )


def wrap_with_resilience(adapter: ChatProvider, settings: Settings) -> ResilientProvider:
    """Give ``adapter`` a retry loop and a breaker of its own."""
    return ResilientProvider(
        inner=adapter,
        policy=build_policy(settings),
        breaker=CircuitBreaker(
            name=adapter.name,
            failure_threshold=settings.breaker_failure_threshold,
            open_seconds=settings.breaker_reset_seconds,
            backoff_multiplier=settings.breaker_backoff_multiplier,
            max_open_seconds=settings.breaker_max_open_seconds,
        ),
    )
