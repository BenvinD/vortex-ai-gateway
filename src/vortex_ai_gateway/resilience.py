"""Retry and circuit-breaking primitives, independent of any provider.

Three pieces, deliberately small and separately testable:

* :class:`RetryPolicy` — how many attempts, how long to wait between them, and
  the wall-clock deadline the whole sequence must fit inside.
* :class:`RetryBudget` — a token bucket that caps retries *in aggregate*. Max
  attempts bounds one request; the budget stops an outage turning every
  client's retries into a load multiplier against a recovering provider
  (ADR-001).
* :class:`CircuitBreaker` — a three-state breaker, one per provider, that stops
  calling an upstream that is failing and probes it with a single trial before
  letting traffic back (ADR-002).

Nothing here knows what a provider is. The wiring lives in
:mod:`vortex_ai_gateway.providers.resilience_wrapper`, which is what makes
these testable without a request, a client or an event loop full of adapters.

Durations use :func:`time.monotonic` throughout, exported as :data:`now` so the
wrapper shares one clock. A wall clock can step backwards under NTP, which
would wedge a breaker open or reopen it early.
"""

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

import structlog

logger = structlog.get_logger(__name__)

#: The one clock every duration in the resilience path is measured against.
now = time.monotonic

#: Emitted on every breaker state change, under one event name so a query for
#: "what tripped today?" is a single filter rather than a union.
BREAKER_EVENT: Final = "circuit breaker transition"


@dataclass
class RetryBudget:
    """A token bucket limiting how many retries may be spent over time.

    ``capacity`` tokens are available at rest and refill at
    ``refill_per_second``. Each retry consumes one; when the bucket is empty
    the caller stops retrying and reports the original failure.

    The point is aggregate protection, not per-request protection. Three
    attempts per request is fine until every request is failing, at which
    point undifferentiated retrying triples the load on the very provider
    that is struggling.
    """

    capacity: int = 10
    refill_per_second: float = 1.0
    _tokens: float = field(init=False, repr=False)
    _last: float = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last = now()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> bool:
        """Take ``tokens`` from the bucket, refilling first. False when empty."""
        async with self._lock:
            current = now()
            elapsed = current - self._last
            if elapsed > 0:
                self._tokens = min(
                    float(self.capacity), self._tokens + elapsed * self.refill_per_second
                )
                self._last = current
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


@dataclass
class RetryPolicy:
    """How a failed call is retried.

    ``deadline_seconds`` is the budget for the *whole* sequence, and it must be
    larger than one attempt's timeout or the first slow failure consumes it and
    ``max_attempts`` becomes a lie — the arithmetic is set out in ADR-001.
    """

    max_attempts: int = 3
    backoff_base: float = 0.5
    max_backoff: float = 10.0
    deadline_seconds: float | None = None
    budget: RetryBudget | None = None

    def backoff_seconds(self, attempt: int) -> float:
        """Undithered exponential backoff for a zero-based attempt number."""
        return min(self.max_backoff, self.backoff_base * (2.0**attempt))

    def backoff_with_jitter(self, attempt: int) -> float:
        """Backoff with *full* jitter: uniform over ``[0, backoff]``.

        Not backoff-plus-noise. Every client that failed in the same instant
        would otherwise retry in the same instant, and a brief outage becomes a
        series of synchronised herds against a recovering provider. Spreading
        uniformly over the whole window is the variant with the numbers behind
        it.
        """
        return random.uniform(0.0, self.backoff_seconds(attempt))


class BreakerState(Enum):
    """Where a breaker is in its cycle."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """One provider's breaker: stop calling something that is failing.

    * **CLOSED** — traffic flows. ``failure_threshold`` consecutive failed
      *requests* open it.
    * **OPEN** — every call is refused immediately, which is the point: a
      refusal costs nothing and frees the worker a timeout would have pinned.
    * **HALF_OPEN** — after the open window, exactly one caller is admitted as
      a trial. Success closes the breaker; failure reopens it for longer.

    The single-trial rule is what makes recovery a trickle rather than the
    whole herd arriving at once. The trial slot is reserved by :meth:`allow`
    and must always be handed back — via :meth:`record_success`,
    :meth:`record_failure` or :meth:`release_trial` — or the breaker would sit
    in ``HALF_OPEN`` refusing everything forever.
    """

    def __init__(
        self,
        name: str = "provider",
        failure_threshold: int = 5,
        open_seconds: float = 60.0,
        backoff_multiplier: float = 2.0,
        max_open_seconds: float = 600.0,
    ) -> None:
        #: Provider this breaker guards, carried on every transition log line.
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.backoff_multiplier = float(backoff_multiplier)
        self.max_open_seconds = float(max_open_seconds)

        self._base_open_seconds = float(open_seconds)
        self._open_seconds = float(open_seconds)
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_until: float | None = None
        self._half_open_in_use = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name!r}, state={self._state.value!r})"

    def _transition(self, to: BreakerState, **fields: object) -> None:
        """Move to ``to`` and log it. Caller must hold the lock."""
        if self._state is to:
            return
        logger.warning(
            BREAKER_EVENT,
            provider=self.name,
            from_state=self._state.value,
            to_state=to.value,
            **fields,
        )
        self._state = to

    def _open(self) -> None:
        """Open the breaker for the current window. Caller must hold the lock."""
        self._opened_until = now() + self._open_seconds
        self._transition(
            BreakerState.OPEN,
            open_seconds=round(self._open_seconds, 2),
            consecutive_failures=self._consecutive_failures,
        )
        self._consecutive_failures = 0
        # Escalate once, here and nowhere else, so the window really does grow
        # by `backoff_multiplier` per reopen rather than skipping steps.
        self._open_seconds = min(
            self._open_seconds * self.backoff_multiplier, self.max_open_seconds
        )

    async def allow(self) -> bool:
        """Whether a call may proceed now, reserving the trial slot if needed."""
        async with self._lock:
            if self._state is BreakerState.CLOSED:
                return True

            if self._state is BreakerState.OPEN:
                if self._opened_until is not None and now() >= self._opened_until:
                    self._transition(BreakerState.HALF_OPEN, open_window_elapsed=True)
                    self._half_open_in_use = True
                    return True
                return False

            # HALF_OPEN: admit one trial, refuse everyone else until it reports.
            if not self._half_open_in_use:
                self._half_open_in_use = True
                return True
            return False

    async def record_success(self) -> None:
        """A call succeeded: close the breaker and reset the open window."""
        async with self._lock:
            self._transition(BreakerState.CLOSED, reason="success")
            self._consecutive_failures = 0
            self._opened_until = None
            self._open_seconds = self._base_open_seconds
            self._half_open_in_use = False

    async def record_failure(self) -> None:
        """A request failed after exhausting its retries."""
        async with self._lock:
            self._consecutive_failures += 1
            if self._state is BreakerState.HALF_OPEN:
                # The trial failed: straight back to open, for longer.
                self._half_open_in_use = False
                self._open()
                return
            if (
                self._state is BreakerState.CLOSED
                and self._consecutive_failures >= self.failure_threshold
            ):
                self._open()

    async def release_trial(self) -> None:
        """Hand back the trial slot without judging the provider.

        For the case where the trial caller went away — a client disconnect
        cancels the task mid-call — so the slot must not be leaked, but nothing
        was learned about the upstream either.
        """
        async with self._lock:
            self._half_open_in_use = False

    async def state(self) -> BreakerState:
        """The current state. A pure read: it changes nothing."""
        async with self._lock:
            return self._state

    async def retry_after_seconds(self) -> float | None:
        """Seconds until the breaker next admits a trial, if it is open."""
        async with self._lock:
            if self._state is BreakerState.OPEN and self._opened_until is not None:
                return max(0.0, self._opened_until - now())
            return None
