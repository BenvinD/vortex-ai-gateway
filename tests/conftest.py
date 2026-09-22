"""Shared test fixtures."""

import asyncio
from collections.abc import Iterator

import pytest
import structlog

from vortex_ai_gateway import metrics


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Stop structlog's global configuration leaking between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    """Stop one test's counters being read as another's.

    Prometheus instruments are process-global by design — there is one
    ``vortex_requests_total`` per worker however many apps that worker builds —
    so without this every assertion in ``test_metrics.py`` would depend on
    which tests ran before it, and on how many. The label budget goes with
    them: it is the same kind of state, and a test that fills it would
    otherwise make a later test's model report as ``other``.
    """
    metrics.reset()
    yield


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every backoff instead of serving it, and record the durations.

    Shared by the retry tests and the retry *metrics* tests, which is why it
    lives here: both want to assert on what a five-minute backoff did without
    waiting five minutes, and a second copy of this would be a second thing to
    keep in step with `resilience.RetryPolicy`.
    """
    recorded: list[float] = []

    async def _capture(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _capture)
    return recorded
