"""Resilient provider wrapper implementing ChatProvider.

Wraps an inner ChatProvider, applying a RetryPolicy to `complete()` and a
per-provider CircuitBreaker. Streams are forwarded unchanged for now.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from typing import TYPE_CHECKING, Any
from vortex_ai_gateway.providers.errors import ProviderError, ProviderUnavailable, ProviderRateLimited
from vortex_ai_gateway.resilience import CircuitBreaker, RetryPolicy, RetryBudget

if TYPE_CHECKING:
    from vortex_ai_gateway.providers.base import ChatProvider


class ResilientProvider:
    """Wrap a ChatProvider with retry and a circuit breaker.

    Behavior:
    - honor the circuit-breaker per-request (breaker consulted each attempt);
    - retries use full jitter and an optional retry budget;
    - non-retryable errors do not count toward the breaker; 429s are treated
      as transient by default (configurable via wrap helper).
    """

    def __init__(self, inner: ChatProvider, policy: RetryPolicy, breaker: CircuitBreaker, max_elapsed: float | None = None) -> None:
        self.inner = inner
        self.policy = policy
        self.breaker = breaker
        self.max_elapsed = float(max_elapsed) if max_elapsed is not None else None
        # expose the same name as the inner provider for logging/response
        self.name = getattr(inner, "name", "resilient")

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        start = time.monotonic()
        last_exc: Exception | None = None

        for attempt in range(self.policy.max_attempts):
            # Before each attempt ensure the breaker allows it
            allowed = await self.breaker.allow()
            if not allowed:
                # include Retry-After if available from breaker
                remaining = await self.breaker.remaining_open_seconds()
                # Map to ProviderUnavailable so routing returns 503
                raise ProviderUnavailable(
                    f"circuit open for provider {self.name}", provider=self.name, status_code=503
                )

            try:
                result = await self.inner.complete(request)
            except asyncio.CancelledError:
                # Cancellation: release any reserved half-open slot and propagate
                await self.breaker.release_trial()
                raise
            except Exception as exc:
                last_exc = exc
                # If it's a ProviderError and retryable, consider a retry
                if isinstance(exc, ProviderError) and getattr(type(exc), "retryable", False):
                    # If this was the half-open trial, fail fast and re-open
                    state = await self.breaker.state()
                    if state == "half_open":
                        await self.breaker.record_failure()
                        raise

                    # Check retry budget (if configured). Budget limits retries only.
                    if self.policy.budget is not None:
                        allowed_retry = await self.policy.budget.consume()
                        if not allowed_retry:
                            break

                    # honour provider-supplied retry_after but cap it
                    retry_after = getattr(exc, "retry_after", None)
                    sleep = None
                    if retry_after is not None:
                        sleep = float(retry_after)
                    else:
                        sleep = self.policy.backoff_with_jitter(attempt)

                    # Cap sleep to remaining allowed time
                    if self.max_elapsed is not None:
                        elapsed = time.monotonic() - start
                        remaining = self.max_elapsed - elapsed
                        if remaining <= 0:
                            break
                        if sleep > remaining:
                            # don't sleep into the deadline; give up
                            break

                    # Only sleep if we'll actually attempt again
                    if attempt < self.policy.max_attempts - 1:
                        await asyncio.sleep(sleep)
                    continue

                # Non-retryable: do not record a breaker failure; re-raise
                raise

            else:
                # success: reset breaker and return
                await self.breaker.record_success()
                return result

        # Exhausted attempts or budget: record one failure for this request
        await self.breaker.record_failure()
        if last_exc is not None:
            raise last_exc
        raise ProviderError("resilient provider exhausted retries", provider=self.name)

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        # Forward the stream directly. A more advanced wrapper could monitor
        # chunk arrival and trip breakers on repeated stream failures.
        return self.inner.stream(request)

    async def aclose(self) -> None:
        closer = getattr(self.inner, "aclose", None)
        if closer is not None:
            await closer()


def wrap_with_resilience(adapter: ChatProvider, name: str, settings) -> ChatProvider:
    """Factory helper: build a ResilientProvider from settings.

    Reads settings using the VORTEX_ names added to config.py. If retry budget
    capacity is zero the budget is disabled.
    """
    policy = RetryPolicy(
        max_attempts=int(getattr(settings, "retry_max_attempts", 3)),
        backoff_base=float(getattr(settings, "retry_backoff_seconds", 0.5)),
        max_backoff=float(getattr(settings, "retry_max_backoff_seconds", 10.0)),
        max_elapsed_seconds=float(getattr(settings, "request_timeout_seconds", 30.0)),
    )

    # optional retry budget
    budget_capacity = int(getattr(settings, "retry_budget_capacity", 0))
    budget_refill = float(getattr(settings, "retry_budget_refill_per_second", 0.0))
    if budget_capacity > 0 and budget_refill > 0.0:
        policy.budget = RetryBudget(capacity=budget_capacity, refill_per_second=budget_refill)

    breaker = CircuitBreaker(
        failure_threshold=int(getattr(settings, "breaker_failure_threshold", 5)),
        open_seconds=float(getattr(settings, "breaker_reset_seconds", 60.0)),
        backoff_multiplier=float(getattr(settings, "breaker_backoff_multiplier", 2.0)),
        max_open_seconds=float(getattr(settings, "breaker_max_open_seconds", 600.0)),
    )
    return ResilientProvider(adapter, policy, breaker, max_elapsed=float(getattr(settings, "request_timeout_seconds", 30.0)))
