from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Use monotonic clocks for durations
_now = time.monotonic

@dataclass
class RetryBudget:
    capacity: int = 10
    refill_per_second: float = 1.0

    def __post_init__(self) -> None:
        self._tokens: float = float(self.capacity)
        self._last: float = _now()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> bool:
        async with self._lock:
            now = _now()
            elapsed = now - self._last
            if elapsed > 0:
                self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
                self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_base: float = 0.5
    max_backoff: float = 10.0
    budget: Optional[RetryBudget] = None
    # optional total elapsed cap for a request (seconds)
    max_elapsed_seconds: Optional[float] = None

    def backoff_seconds(self, attempt: int) -> float:
        return min(self.max_backoff, self.backoff_base * (2 ** attempt))

    def backoff_with_jitter(self, attempt: int) -> float:
        base = self.backoff_seconds(attempt)
        return random.uniform(0.0, base)

class _CBState(Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, open_seconds: float = 60.0, backoff_multiplier: float = 2.0, max_open_seconds: float = 600.0) -> None:
        self.failure_threshold = int(failure_threshold)
        self._base_open_seconds = float(open_seconds)
        self.backoff_multiplier = float(backoff_multiplier)
        self._max_open_seconds = float(max_open_seconds)
        self._state: _CBState = _CBState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_until: float | None = None
        self._current_open_seconds: float = float(open_seconds)
        self._lock = asyncio.Lock()
        self._half_open_in_use: bool = False

    async def allow(self) -> bool:
        async with self._lock:
            now = _now()
            if self._state is _CBState.CLOSED:
                return True
            if self._state is _CBState.OPEN:
                if self._opened_until is not None and now >= self._opened_until:
                    # transition to half-open and reserve the trial slot
                    self._state = _CBState.HALF_OPEN
                    self._half_open_in_use = True
                    return True
                return False
            # HALF_OPEN
            if self._state is _CBState.HALF_OPEN:
                if not self._half_open_in_use:
                    self._half_open_in_use = True
                    return True
                return False
            return False

    async def _open(self) -> None:
        self._state = _CBState.OPEN
        # cap the open window
        self._current_open_seconds = min(self._current_open_seconds, self._max_open_seconds)
        self._opened_until = _now() + self._current_open_seconds

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._state is _CBState.HALF_OPEN:
                # trial failed: re-open immediately and grow the open window
                self._current_open_seconds = min(self._current_open_seconds * self.backoff_multiplier, self._max_open_seconds)
                self._half_open_in_use = False
                await self._open()
                self._consecutive_failures = 0
                return
            if self._state is _CBState.CLOSED and self._consecutive_failures >= self.failure_threshold:
                # open the breaker
                await self._open()
                # increase the open period for subsequent re-opens
                self._current_open_seconds = min(self._current_open_seconds * self.backoff_multiplier, self._max_open_seconds)
                self._consecutive_failures = 0

    async def release_trial(self) -> None:
        """Release the half-open trial slot (e.g., on cancellation) without changing state."""
        async with self._lock:
            self._half_open_in_use = False

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._state = _CBState.CLOSED
            self._opened_until = None
            self._current_open_seconds = float(self._base_open_seconds)
            self._half_open_in_use = False

    async def is_open(self) -> bool:
        async with self._lock:
            if self._state is _CBState.OPEN and self._opened_until is not None and _now() >= self._opened_until:
                # move to half-open
                self._state = _CBState.HALF_OPEN
                self._half_open_in_use = False
                return False
            return self._state is _CBState.OPEN

    async def remaining_open_seconds(self) -> float | None:
        async with self._lock:
            if self._state is _CBState.OPEN and self._opened_until is not None:
                return max(0.0, self._opened_until - _now())
            return None

    async def state(self) -> str:
        async with self._lock:
            return self._state.value
