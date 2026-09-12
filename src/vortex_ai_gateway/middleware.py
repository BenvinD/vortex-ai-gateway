"""ASGI middleware for the gateway."""

import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vortex_ai_gateway.cache import CACHE_HEADER

REQUEST_ID_HEADER = "x-request-id"


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
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, cache_outcome
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[self.header_name] = request_id
                cache_outcome = headers.get(CACHE_HEADER)
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


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
