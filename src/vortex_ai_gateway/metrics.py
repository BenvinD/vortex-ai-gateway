"""Prometheus instruments, and the cap that keeps them from growing forever.

Everything the gateway counts lives here, in one registry of its own rather
than ``prometheus_client``'s global default — a library that registers a
collector on import would otherwise end up on this gateway's ``/metrics``, and
what a scrape endpoint publishes should be a list somebody wrote down.

The instruments are module-level singletons because a Prometheus counter is
process-global by nature: there is one ``vortex_requests_total`` per worker no
matter how many :func:`~vortex_ai_gateway.gateway.create_app` calls that worker
made. :func:`reset` exists for tests, which make a great many of them.

**The counters are per worker process**, like the circuit breaker's state and
both caches' ``CacheStats``. Running uvicorn with ``--workers 2`` gives two
processes with two sets of counters and one ``/metrics`` port, so a scrape sees
whichever worker answered. ``prometheus_client`` has a multiprocess mode for
exactly this and it is deliberately not wired up: it needs a shared directory,
it changes what a gauge means, and the gateway has other per-worker state that
it would not fix. Run one worker per port and let the orchestrator scale.

**Cardinality is the failure mode this module is designed around.** A
Prometheus label value allocates a time series that lives until the process
exits, and ``model`` — the single most useful breakdown on every metric here —
is a free-text field in a request body. A client looping over random model
names would otherwise turn the monitoring system into the outage. So every
caller-supplied label goes through a :class:`LabelBudget`, which admits a fixed
number of distinct values and calls the rest ``other`` (ADR-027).
"""

from __future__ import annotations

import threading
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.metrics import MetricWrapperBase

#: What a label is reported as when the request did not say, or had not got far
#: enough to know. Distinct from ``other``: nobody chose this value.
UNKNOWN: Final = "unknown"

#: What a caller-supplied label collapses to once its budget is spent.
OVERFLOW: Final = "other"

#: The gateway's own registry. Nothing is registered here that this module did
#: not declare, so ``/metrics`` is reviewable by reading one file.
REGISTRY: Final = CollectorRegistry()

# Drop the `_created` companion series. ``prometheus_client`` emits one per
# counter and histogram, carrying the Unix time the series was first
# observed — an OpenMetrics feature that Prometheus's own text scrape
# ignores. Left on it silently doubles the number of series this endpoint
# publishes, which is the exact quantity ADR-027 exists to keep an eye on.
# The ignore is narrow and deliberate: `prometheus_client` ships `py.typed`
# but leaves this one helper unannotated, so strict mode reads the call as
# untyped. A blanket `ignore_missing_imports` for the package would turn
# every other use of it into `Any`.
disable_created_metrics()  # type: ignore[no-untyped-call]

#: Latency buckets in seconds. Stretched much further to the right than a
#: typical web service's: a completion that takes forty seconds is slow, not
#: broken, and a histogram whose last bucket is ``1.0`` reports every LLM call
#: as "+Inf" and can no longer tell a slow day from an outage.
DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    float("inf"),
)

#: Time-to-first-token buckets. Shorter tail than the full duration: TTFT is
#: what a user experiences as "did it hang", and the interesting resolution is
#: all under a few seconds.
TTFT_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, float("inf"))


class LabelBudget:
    """Admit at most ``limit`` distinct values for one label; the rest are ``other``.

    The point is not tidiness. A label value the gateway has never seen before
    allocates a new time series in every metric it appears on, and nothing ever
    frees it — so an unbounded label is a memory leak whose rate is chosen by
    whoever is calling. The budget makes the worst case a number an operator
    picked.

    Values already admitted keep working forever, so the breakdown stays stable
    once traffic settles: the first ``limit`` models seen are the ones the
    dashboard will be about. Which is also the weakness, and it is recorded in
    ADR-027 — a model introduced after the budget fills reports as ``other``
    until the process restarts.

    Guarded by a lock because a sync endpoint runs in a worker thread, so this
    is not always reached from the event loop.
    """

    def __init__(self, limit: int, *, overflow: str = OVERFLOW) -> None:
        if limit < 1:
            raise ValueError(f"a label budget needs room for at least one value, got {limit!r}")
        self.limit = limit
        self.overflow = overflow
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"LabelBudget(limit={self.limit}, seen={len(self._seen)})"

    @property
    def seen(self) -> frozenset[str]:
        """The values admitted so far, for tests and for a debug endpoint."""
        with self._lock:
            return frozenset(self._seen)

    def __call__(self, value: str | None) -> str:
        """``value`` if there is room for it, ``unknown`` if empty, else ``other``."""
        if not value:
            return UNKNOWN
        with self._lock:
            if value in self._seen:
                return value
            if len(self._seen) < self.limit:
                self._seen.add(value)
                return value
        return self.overflow

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


#: The default cap, replaced by :func:`configure` when settings say otherwise.
DEFAULT_LABEL_BUDGET: Final = 50

#: Caller-supplied, so capped. ``provider`` is *not* capped anywhere here: a
#: provider name can only come from the routing table, which an operator wrote.
model_label = LabelBudget(DEFAULT_LABEL_BUDGET)


def configure(*, label_budget: int) -> None:
    """Resize the model-label budget. Called once by ``create_app``."""
    global model_label
    if label_budget != model_label.limit:
        model_label = LabelBudget(label_budget)


# --- the instruments -----------------------------------------------------
#
# Named `vortex_*` throughout, units in the name, and `_total` on every
# counter — the Prometheus conventions, which matter because they are what a
# recording rule, an alert and a Grafana query all assume.

REQUESTS: Final = Counter(
    "vortex_requests_total",
    "HTTP requests served, by route and outcome.",
    ("route", "method", "status", "model", "provider"),
    registry=REGISTRY,
)

DURATION: Final = Histogram(
    "vortex_request_duration_seconds",
    "Wall-clock time to serve a request, first byte in to last byte out.",
    ("route", "model", "provider"),
    buckets=DURATION_BUCKETS,
    registry=REGISTRY,
)

TTFT: Final = Histogram(
    "vortex_stream_ttft_seconds",
    "Time from a streaming request arriving to its first non-empty body chunk.",
    ("model", "provider"),
    buckets=TTFT_BUCKETS,
    registry=REGISTRY,
)

STREAMS: Final = Counter(
    "vortex_streams_total",
    "Streamed responses by how they ended: completed, failed or abandoned.",
    ("provider", "model", "outcome"),
    registry=REGISTRY,
)

CACHE_LOOKUPS: Final = Counter(
    "vortex_cache_lookups_total",
    "Cache outcomes per tier, as reported on the response headers.",
    ("tier", "outcome"),
    registry=REGISTRY,
)

PROVIDER_ATTEMPTS: Final = Counter(
    "vortex_provider_attempts_total",
    "Individual upstream calls, including every retry of the same request.",
    ("provider", "outcome"),
    registry=REGISTRY,
)

PROVIDER_RETRIES: Final = Counter(
    "vortex_provider_retries_total",
    "Retries actually slept for and re-issued, by the failure that caused them.",
    ("provider", "error"),
    registry=REGISTRY,
)

PROVIDER_GIVE_UPS: Final = Counter(
    "vortex_provider_give_ups_total",
    "Requests a provider stopped retrying, by why it stopped.",
    ("provider", "reason"),
    registry=REGISTRY,
)

FALLBACKS: Final = Counter(
    "vortex_fallbacks_total",
    "Hops from one provider to the next in a fallback chain.",
    ("from_provider", "to_provider"),
    registry=REGISTRY,
)

BREAKER_TRANSITIONS: Final = Counter(
    "vortex_breaker_transitions_total",
    "Circuit breaker state changes, by the state entered.",
    ("provider", "state"),
    registry=REGISTRY,
)

BREAKER_STATE: Final = Gauge(
    "vortex_breaker_state",
    "Circuit breaker state now: 0 closed, 1 half-open, 2 open.",
    ("provider",),
    registry=REGISTRY,
)

TOKENS: Final = Counter(
    "vortex_tokens_total",
    "Tokens settled against the caller's model, split prompt and completion.",
    ("model", "kind"),
    registry=REGISTRY,
)

COST: Final = Counter(
    "vortex_cost_usd_total",
    "What those tokens cost at the current price table, in USD.",
    ("model",),
    registry=REGISTRY,
)

BUILD_INFO: Final = Gauge(
    "vortex_build_info",
    "Always 1; the labels are the point.",
    ("version", "environment"),
    registry=REGISTRY,
)

#: Every instrument, for :func:`reset`. Listed rather than discovered from the
#: registry so a collector added by accident is not silently reset along with
#: the ones that belong here.
ALL: Final[tuple[MetricWrapperBase, ...]] = (
    REQUESTS,
    DURATION,
    TTFT,
    STREAMS,
    CACHE_LOOKUPS,
    PROVIDER_ATTEMPTS,
    PROVIDER_RETRIES,
    PROVIDER_GIVE_UPS,
    FALLBACKS,
    BREAKER_TRANSITIONS,
    BREAKER_STATE,
    TOKENS,
    COST,
    BUILD_INFO,
)


def reset() -> None:
    """Drop every recorded series and the label budget with them.

    For tests only. Production never calls this: a counter that resets is a
    counter ``rate()`` reads as a restart, and Prometheus handles a real
    restart correctly precisely because the process also drops its start time.
    """
    for metric in ALL:
        metric.clear()
    model_label.clear()


def render() -> tuple[bytes, str]:
    """The scrape body and its content type.

    Plain text, version 0.0.4 — what Prometheus asks for when it scrapes and
    what ``generate_latest`` produces. OpenMetrics is a content negotiation
    this endpoint does not do.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
