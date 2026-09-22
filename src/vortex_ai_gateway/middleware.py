"""ASGI middleware for the gateway.

Two, and the order they are mounted in is load-bearing.
:class:`RequestIDMiddleware` is the outer one because it *clears* structlog's
context vars on entry; anything bound outside it would be wiped before a single
handler ran. :class:`TelemetryMiddleware` sits just inside, which is what lets
it bind the trace and span IDs into the same context the request ID already
rides in, so every log line a request emits can be pasted into a trace viewer.
The cost is that the server span misses the microseconds the outer middleware
spends, and the access log's ``duration_ms`` is always a shade larger than the
span — a trade recorded in ADR-026.

Both are raw ASGI rather than ``BaseHTTPMiddleware``: that base class buffers
through a second task, which breaks streaming responses and adds a hop to every
request. It matters twice over here, because the ASGI boundary is the only
place that can see a response's *first body message* leave, and that is the
only honest definition of time to first token.
"""

import time
from typing import Final
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vortex_ai_gateway import metrics, tracing
from vortex_ai_gateway.cache import CACHE_HEADER
from vortex_ai_gateway.semantic import SEMANTIC_HEADER, SEMANTIC_SCORE_HEADER

REQUEST_ID_HEADER = "x-request-id"

#: The media type that makes a response a stream, and so the one whose first
#: body message is worth timing.
SSE_MEDIA_TYPE: Final = "text/event-stream"

#: The route label used when nothing matched — a 404, a probe from a scanner,
#: a path with a typo. One series for all of them, which is the point: an
#: unmatched path is caller-supplied and would otherwise be unbounded.
UNMATCHED_ROUTE: Final = "unmatched"


class RequestIDMiddleware:
    """Give every HTTP request a request ID and log its completion.

    The ID is taken from the inbound ``X-Request-ID`` header when present (so a
    trace survives a hop between services) and is otherwise a fresh UUID. For
    the lifetime of the request it is:

    * bound into structlog's context vars, so every log line emitted while the
      request is handled carries ``request_id``;
    * stored on ``request.state.request_id`` for handlers that want it;
    * echoed back in the ``X-Request-ID`` response header.

    The completion line also carries the response's ``X-Cache`` outcome, read
    off the outgoing headers rather than passed down from the handler. That is
    what makes a hit rate a log query: the counters in
    :class:`~vortex_ai_gateway.cache.CacheStats` say how *one worker* is doing
    and are lost when it exits, where a per-request field aggregates across the
    fleet and survives in whatever already stores the logs. Reading the header
    also means a response served from anywhere — including one this middleware
    knows nothing about — is described the same way.

    Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it stays correct
    for streaming responses and adds no task hop.
    """

    def __init__(self, app: ASGIApp, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header_name, "").strip()
        request_id = inbound or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        cache_outcome: str | None = None
        semantic_outcome: str | None = None
        semantic_score: str | None = None
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, cache_outcome, semantic_outcome, semantic_score
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[self.header_name] = request_id
                cache_outcome = headers.get(CACHE_HEADER)
                semantic_outcome = headers.get(SEMANTIC_HEADER)
                semantic_score = headers.get(SEMANTIC_SCORE_HEADER)
            await send(message)

        logger = structlog.get_logger("vortex_ai_gateway.access")
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "request failed",
                http_method=scope["method"],
                http_path=scope["path"],
                duration_ms=_elapsed_ms(started),
                **_cache_field(cache_outcome),
                **_semantic_field(semantic_outcome, semantic_score),
            )
            raise
        else:
            logger.info(
                "request completed",
                http_method=scope["method"],
                http_path=scope["path"],
                http_status=status_code,
                duration_ms=_elapsed_ms(started),
                **_cache_field(cache_outcome),
                **_semantic_field(semantic_outcome, semantic_score),
            )
        finally:
            structlog.contextvars.clear_contextvars()


def _cache_field(outcome: str | None) -> dict[str, str]:
    """The ``cache`` field, or nothing at all when there is no cache.

    Absent rather than ``null``, for the reason the header itself is absent: a
    field naming a subsystem this deployment does not run is noise on every
    line. It also keeps the arithmetic honest — a hit rate is
    ``cache=HIT`` over ``cache=HIT`` plus ``cache=MISS``, and a run of nulls
    from a cacheless deployment cannot land in either total.
    """
    return {"cache": outcome} if outcome is not None else {}


def _semantic_field(outcome: str | None, score: str | None) -> dict[str, str | float]:
    """The ``semantic_cache`` field and, when there was a nearest entry, its score.

    The score is what ADR-005's threshold is tuned against, and the access log
    is where every request's score lands — a miss at ``0.93`` under a ``0.95``
    threshold is either a paraphrase the cache should have caught or a
    different question it was right to refuse, and only a person reading a
    sample of them can say which.
    """
    if outcome is None:
        return {}
    fields: dict[str, str | float] = {"semantic_cache": outcome}
    if score is not None:
        fields["semantic_score"] = float(score)
    return fields


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class TelemetryMiddleware:
    """Open one span per request, and count what the request did.

    Mounted *inside* :class:`RequestIDMiddleware` — see the module docstring
    for why — and it does three things that all want the same clock:

    * **A server span**, named for the matched route rather than the raw path,
      so ``/v1/chat/completions`` is one span name and not one per request. It
      is the parent of the cache, provider and retry-attempt spans created
      further down, which is what turns a retry storm from three log lines into
      a shape.
    * **The request counters and histograms** in
      :mod:`~vortex_ai_gateway.metrics`. Recorded here unconditionally, because
      an atomic increment is cheaper than the branch that would skip it;
      ``metrics_enabled`` decides whether anyone can *read* them (ADR-028).
    * **Time to first token**, for streams only. This is the one measurement
      that exists nowhere else: it is the gap between the request arriving and
      the first ``http.response.body`` message with bytes in it, and no layer
      above or below the ASGI boundary is handed that message.

    Cache outcomes are read off the outgoing headers, exactly as the access log
    reads them, so there is one definition of "a hit" and it is the same one
    the caller was told (ADR-027).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope["method"]
        started = time.perf_counter()
        status_code = 500
        streamed = False
        ttft: float | None = None
        cache_outcome: str | None = None
        semantic_outcome: str | None = None
        semantic_score: str | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, streamed, ttft, cache_outcome, semantic_outcome, semantic_score
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = Headers(raw=message["headers"])
                cache_outcome = headers.get(CACHE_HEADER)
                semantic_outcome = headers.get(SEMANTIC_HEADER)
                semantic_score = headers.get(SEMANTIC_SCORE_HEADER)
                streamed = headers.get("content-type", "").startswith(SSE_MEDIA_TYPE)
            elif message["type"] == "http.response.body" and ttft is None and message.get("body"):
                # First message carrying bytes, not first message: Starlette
                # sends an empty body message to close a response, and timing
                # that would report a stream that produced nothing as instant.
                ttft = time.perf_counter() - started
            await send(message)

        with tracing.span(
            f"{method} {scope['path']}",
            {
                "http.request.method": method,
                "url.path": scope["path"],
                "vortex.request_id": scope.get("state", {}).get("request_id"),
            },
        ) as span:
            structlog.contextvars.bind_contextvars(**tracing.trace_context())
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                state = scope.get("state", {})
                route = _route_label(scope)
                model = metrics.model_label(state.get("model"))
                provider = state.get("provider") or metrics.UNKNOWN
                elapsed = time.perf_counter() - started

                span.update_name(f"{method} {route}")
                _describe(
                    span,
                    status_code=status_code,
                    route=route,
                    model=state.get("model"),
                    provider=state.get("provider"),
                    cache=cache_outcome,
                    semantic=semantic_outcome,
                    semantic_score=semantic_score,
                )

                metrics.REQUESTS.labels(
                    route=route,
                    method=method,
                    status=str(status_code),
                    model=model,
                    provider=provider,
                ).inc()
                metrics.DURATION.labels(route=route, model=model, provider=provider).observe(
                    elapsed
                )
                if streamed and ttft is not None:
                    metrics.TTFT.labels(model=model, provider=provider).observe(ttft)
                _count_cache(cache_outcome, semantic_outcome)


def _route_label(scope: Scope) -> str:
    """The matched route's template, or one shared label for everything else.

    The *template* — ``/v1/chat/completions``, never the path as sent — because
    a label built from the request line is a time series per distinct URL, and
    the things that send distinct URLs are scanners.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


def _count_cache(exact: str | None, semantic: str | None) -> None:
    """One counter increment per tier that reported an outcome.

    Absent means the tier is not running, and it is not counted at all — the
    same rule the access log's ``cache`` field follows, and for the same
    reason: a deployment with no cache must not contribute zeros to somebody
    else's hit rate.
    """
    for tier, outcome in (("exact", exact), ("semantic", semantic)):
        if outcome is not None:
            metrics.CACHE_LOOKUPS.labels(tier=tier, outcome=outcome).inc()


def _describe(
    span: tracing.Span,
    *,
    status_code: int,
    route: str,
    model: str | None,
    provider: str | None,
    cache: str | None,
    semantic: str | None,
    semantic_score: str | None,
) -> None:
    """Put the request's outcome on the span, skipping what is not known.

    The model and provider go on **unbudgeted**, unlike their metric labels: a
    span attribute is a string on one span, not a time series that lives
    forever, so the cap that ADR-027 exists to impose would only make traces
    less useful. A ``5xx`` sets the span's status, since a handled failure
    returns normally and would otherwise leave the span looking successful.
    """
    if not span.is_recording():
        return
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("http.route", route)
    for key, value in (
        ("vortex.model", model),
        ("vortex.provider", provider),
        ("vortex.cache", cache),
        ("vortex.semantic_cache", semantic),
    ):
        if value is not None:
            span.set_attribute(key, value)
    if semantic_score is not None:
        span.set_attribute("vortex.semantic_cache.score", float(semantic_score))
    if status_code >= 500:
        span.set_status(tracing.StatusCode.ERROR, f"HTTP {status_code}")
