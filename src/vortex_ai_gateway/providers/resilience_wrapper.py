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

Streaming is forwarded untouched by both. Once a chunk is on the wire the
``200`` is committed, so a mid-stream retry or hop would splice two
generations into one response; establishing-phase retries are worth doing and
are not done here yet.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Final

import structlog

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.errors import CircuitOpenError, ProviderError
from vortex_ai_gateway.resilience import CircuitBreaker, RetryBudget, RetryPolicy, now

logger = structlog.get_logger(__name__)

#: One event name per kind, so "how often did we retry?" and "what fell back?"
#: are each a single log filter (the pattern `streaming.STREAM_EVENT` uses).
RETRY_EVENT: Final = "provider call retried"
GIVE_UP_EVENT: Final = "provider call failed after retries"
FALLBACK_EVENT: Final = "provider fallback"


def _retry_after_of(exc: ProviderError) -> float | None:
    """The wait a provider asked for, when it named one.

    Only rate limits and an open breaker carry it, and both are typed, but a
    duck-typed read keeps this from having to import every error that might
    grow the attribute later.
    """
    value = getattr(exc, "retry_after", None)
    return float(value) if isinstance(value, int | float) else None


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
        started = now()
        deadline = self.policy.deadline_seconds
        last_exc: ProviderError | None = None

        for attempt in range(self.policy.max_attempts):
            # Consulted before *every* attempt, not once per request: when the
            # breaker opens mid-sequence the remaining attempts must stop.
            if not await self.breaker.allow():
                raise await self._refuse()

            try:
                completion = await self.inner.complete(request)
            except asyncio.CancelledError:
                # The caller went away. Hand back a half-open trial slot without
                # blaming the upstream, then let the cancellation carry on.
                await self.breaker.release_trial()
                raise
            except ProviderError as exc:
                if not exc.retryable:
                    # The caller's own bad request, our bad key, a body we
                    # cannot read. None of these say the provider is unhealthy,
                    # so none of them may move the breaker.
                    raise
                last_exc = exc
                if not await self._should_retry(exc, attempt, started, deadline):
                    break
            else:
                await self.breaker.record_success()
                return completion

        await self.breaker.record_failure()
        if last_exc is None:  # pragma: no cover - defensive; the loop always sets it
            raise CircuitOpenError(f"{self.name} exhausted its retries", provider=self.name)
        logger.warning(
            GIVE_UP_EVENT,
            provider=self.name,
            model=request.model,
            attempts=self.policy.max_attempts,
            error=type(last_exc).__name__,
            code=last_exc.code,
        )
        raise last_exc

    async def _should_retry(
        self,
        exc: ProviderError,
        attempt: int,
        started: float,
        deadline: float | None,
    ) -> bool:
        """Wait and report whether another attempt should follow this failure."""
        if attempt >= self.policy.max_attempts - 1:
            return False

        # A budget protects the provider in aggregate; per-request attempt
        # counts cannot. Consumed only for retries, never the first call.
        if self.policy.budget is not None and not await self.policy.budget.consume():
            logger.warning(
                GIVE_UP_EVENT,
                provider=self.name,
                reason="retry budget exhausted",
                error=type(exc).__name__,
            )
            return False

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
                logger.warning(
                    GIVE_UP_EVENT,
                    provider=self.name,
                    reason="deadline reached",
                    error=type(exc).__name__,
                    remaining_s=round(max(0.0, remaining), 3),
                )
                return False

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
        await asyncio.sleep(sleep_for)
        return True

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Forward the stream untouched (see the module docstring)."""
        return self.inner.stream(request)

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
        last_exc: ProviderError | None = None
        for position, provider in enumerate(self.providers):
            try:
                return await provider.complete(request)
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

        if last_exc is None:  # pragma: no cover - the loop cannot end without one
            raise ValueError("a fallback chain needs at least one provider")
        raise last_exc

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Stream from the head of the chain only (see the module docstring)."""
        return self.providers[0].stream(request)

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
