"""The OpenAI-compatible HTTP surface.

The router is thin on purpose. It owns three things a provider must not: the
JSON/SSE framing, the mapping from an upstream failure to an HTTP status, and
the decision to stream. Everything else is delegated — validation to the
contract models, generation to the injected
:class:`~vortex_ai_gateway.providers.base.ChatProvider`.

The status mapping is the reason the provider taxonomy exists (ADR-015). It
lives here, not in the providers package, because an adapter must never have to
know what HTTP status its caller will choose — and because the same
classification has to serve the retry policy, which has no HTTP status at all.

What a stream *cost* is not framing, so it is not here either:
:mod:`~vortex_ai_gateway.streaming` owns the accounting, and this module owns
only the decision to hand each chunk to the client. The same applies to what a
caller is *allowed* to spend: :mod:`~vortex_ai_gateway.metering` decides, and
the two calls it exposes — admit before, settle after — are all this module
knows about rate limits and ledgers.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from vortex_ai_gateway.auth import Principal, require_api_key
from vortex_ai_gateway.contracts import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorDetail,
    ErrorResponse,
    ErrorType,
    UsageReport,
)
from vortex_ai_gateway.metering import Meter, Reservation
from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.errors import (
    CircuitOpenError,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from vortex_ai_gateway.routing import UnroutableModelError
from vortex_ai_gateway.streaming import StreamRecord, aclose_stream, metered, wants_usage

logger = structlog.get_logger(__name__)

#: Terminator every OpenAI-compatible SSE stream ends with. Clients stop
#: reading on it rather than on the connection closing.
SSE_DONE = "data: [DONE]\n\n"

#: How each provider failure is reported. Checked in order, so a subclass may
#: precede its parent; anything unmatched is a ``502``.
#:
#: ``CircuitOpenError`` is the one status the gateway raises about *itself*: a
#: ``503`` with ``Retry-After``, because no call was made at all.
#:
#: Two of these are deliberate refusals to pass the upstream status through.
#: A ``401`` from a vendor means *our* key is wrong, so reporting ``401`` would
#: tell the caller to fix a key that is perfectly good; and an upstream timeout
#: is a gateway timeout, ``504``, not a ``500``.
FAILURE_STATUSES: tuple[tuple[type[ProviderError], int, ErrorType], ...] = (
    (UnroutableModelError, status.HTTP_404_NOT_FOUND, "invalid_request_error"),
    (ProviderBadRequest, status.HTTP_400_BAD_REQUEST, "invalid_request_error"),
    (ProviderRateLimited, status.HTTP_429_TOO_MANY_REQUESTS, "rate_limit_error"),
    (ProviderTimeout, status.HTTP_504_GATEWAY_TIMEOUT, "api_error"),
    (CircuitOpenError, status.HTTP_503_SERVICE_UNAVAILABLE, "api_error"),
    (ProviderAuthError, status.HTTP_502_BAD_GATEWAY, "api_error"),
)


def _sse(payload: str) -> str:
    """Frame one JSON document as a server-sent event."""
    return f"data: {payload}\n\n"


def _failure(exc: Exception) -> tuple[int, ErrorResponse]:
    """Turn a failure into the status and envelope the caller should see."""
    if not isinstance(exc, ProviderError):
        return status.HTTP_502_BAD_GATEWAY, ErrorResponse(
            error=ErrorDetail(
                message=f"The upstream provider failed to serve this request: {exc}",
                type="api_error",
                code=type(exc).__name__,
            )
        )

    http_status: int = status.HTTP_502_BAD_GATEWAY
    error_type: ErrorType = "api_error"
    for failure_type, mapped_status, mapped_error in FAILURE_STATUSES:
        if isinstance(exc, failure_type):
            http_status, error_type = mapped_status, mapped_error
            break

    return http_status, ErrorResponse(
        error=ErrorDetail(
            message=str(exc),
            type=error_type,
            # `param` is set only by the failures that know which field was at
            # fault — an unsupported parameter names itself, and that is the
            # difference between a fixable 400 and a mysterious one.
            param=getattr(exc, "parameter", None),
            code=exc.code or type(exc).__name__,
        )
    )


#: How far back ``GET /v1/usage`` looks when the caller does not say. A week is
#: long enough to see a trend and short enough to answer in one round trip per
#: day of history.
DEFAULT_USAGE_DAYS = 7


def create_chat_router(provider: ChatProvider, meter: Meter) -> APIRouter:
    """Build the ``/v1`` router served by ``provider``, metered by ``meter``.

    Both are injected rather than looked up, so a test can mount the same routes
    over a scripted double with no patching — and so a gateway with no Redis
    gets a pass-through :class:`~vortex_ai_gateway.metering.Meter` instead of a
    branch on every request.
    """
    # Auth is declared on the router, not per route, so a route added later
    # cannot be published unauthenticated by omission. The health probes live
    # on the app rather than here precisely so they stay open.
    router = APIRouter(
        prefix="/v1",
        tags=["chat"],
        dependencies=[Depends(require_api_key)],
        responses={401: {"model": ErrorResponse}},
    )

    @router.post(
        "/chat/completions",
        response_model=None,
        responses={
            200: {"model": ChatCompletionResponse},
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def create_chat_completion(
        request: ChatCompletionRequest, http_request: Request
    ) -> Response:
        """Serve a chat completion, streamed or whole.

        A streaming request cannot report a failure with a status code — the
        ``200`` is already on the wire by the time the provider breaks — so the
        two paths diverge here: a ``502`` envelope for the buffered case, an
        error event mid-stream for the other.

        Admission happens before either path, and before the provider is
        touched: the whole point of a limit is that an over-quota request costs
        nothing upstream. Its headers ride on the response whether or not it was
        allowed, so a caller can see their remaining allowance shrinking instead
        of discovering it at zero.
        """
        principal: Principal = http_request.state.principal
        reservation = await meter.admit(principal, request)

        if request.stream:
            return StreamingResponse(
                _stream_events(provider, request, meter, reservation),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                    **reservation.headers,
                },
            )

        try:
            completion = await provider.complete(request)
        except Exception as exc:
            logger.exception("provider request failed", provider=provider.name, model=request.model)
            await meter.discard(reservation)
            http_status, envelope = _failure(exc)
            retry_after = getattr(exc, "retry_after", None)
            headers = dict(reservation.headers)
            if retry_after:
                headers["Retry-After"] = str(int(retry_after))
            return JSONResponse(
                status_code=http_status,
                content=envelope.model_dump(mode="json"),
                headers=headers or None,
            )

        await meter.settle(reservation, completion.usage)
        return JSONResponse(
            content=completion.model_dump(mode="json", exclude_none=True),
            headers=reservation.headers or None,
        )

    @router.get("/usage", response_model=UsageReport)
    async def read_usage(
        http_request: Request,
        days: int = Query(default=DEFAULT_USAGE_DAYS, ge=1, le=365),
    ) -> UsageReport:
        """What the calling key has used, and what it cost.

        Only ever the caller's own usage. There is no key parameter, deliberately:
        a report that can name another key is an authorisation system, and this
        gateway does not have one — every key is equal, so the only safe scope is
        "yours".

        With no ledger configured this answers an empty report rather than a
        ``404``. The endpoint exists either way, so a client can be written once.
        """
        principal: Principal = http_request.state.principal
        if meter.ledger is None:
            return UsageReport(key_id=principal.key_id, start_date=_today(), end_date=_today())
        return await meter.ledger.report(principal.key_id, days=days)

    return router


def _today() -> str:
    """The current UTC day, for a report with no ledger behind it to be about."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


async def _stream_events(
    provider: ChatProvider,
    request: ChatCompletionRequest,
    meter: Meter,
    reservation: Reservation,
) -> AsyncIterator[str]:
    """Yield the SSE body for a streaming completion.

    Three ways this ends, and all three have to leave the same trail — a
    :class:`~vortex_ai_gateway.streaming.StreamRecord` naming what the request
    cost — because the gateway is billed for the tokens whichever way it went:

    * **Completed.** The provider ran out of chunks; the sentinel goes out.
    * **Failed.** The status line is long gone, so the error is emitted as one
      final event before the sentinel. That is the only way the client learns
      the stream was truncated rather than finished.
    * **Abandoned.** The client hung up and Starlette cancelled this task. The
      cancellation is recorded and *re-raised*, so it carries on into the
      provider's own generator, whose ``finally`` closes the upstream
      connection and stops tokens nobody will read (ADR-019). Catching it to
      return quietly would leave the upstream generating at our expense.

    ``CancelledError`` and ``GeneratorExit`` are named explicitly rather than
    left to ``except Exception``: they are ``BaseException``, so the failure
    branch already misses them, and a reader should not have to know that to
    see that an abandonment is handled.
    """
    record = StreamRecord(provider=provider.name, model=request.model)
    forward_usage = wants_usage(request)
    # The gateway asks for usage on every stream; only the forwarding is
    # conditional. See `streaming.metered`.
    stream = provider.stream(metered(request))
    try:
        try:
            async for chunk in stream:
                forwarded = record.observe(chunk, forward_usage=forward_usage)
                if forwarded is not None:
                    yield _sse(forwarded.model_dump_json(exclude_none=True))
        except asyncio.CancelledError, GeneratorExit:
            record.outcome = "abandoned"
            raise
        except Exception as exc:
            record.outcome = "failed"
            logger.exception("provider stream failed", provider=provider.name, model=request.model)
            yield _sse(_failure(exc)[1].model_dump_json())

        yield SSE_DONE
    finally:
        # The bill first, and unconditionally: everything below this line may
        # itself be cancelled, and a stream that is never accounted for is the
        # one failure this whole path exists to prevent.
        record.log()
        try:
            await _settle_stream(meter, reservation, record)
        finally:
            await aclose_stream(stream)


#: Settlements spawned off an abandoned stream, held until they finish. Without
#: a strong reference the event loop may collect the task mid-write, which is a
#: lost ledger entry that nothing reports.
_PENDING_SETTLEMENTS: set[asyncio.Task[None]] = set()


async def _settle_stream(meter: Meter, reservation: Reservation, record: StreamRecord) -> None:
    """Release the stream's reservation and record what it actually cost.

    A stream that failed before its first chunk generated nothing, so its
    reservation comes back in full. Any other ending produced tokens — possibly
    a number nobody has, which
    :meth:`~vortex_ai_gateway.metering.Meter.settle` records as *unmetered*
    rather than as free.

    The abandoned case is spawned rather than awaited, and that asymmetry is the
    whole point of this function existing. For an abandoned stream this code
    runs inside a scope Starlette has already cancelled, where the next ``await``
    that yields raises :class:`asyncio.CancelledError` immediately — so awaiting
    Redis here silently loses the settlement of precisely the request that most
    needs one: the stream nobody has a token count for, whose reservation would
    otherwise stay held for the rest of the window. Measured, not assumed: with
    the settlement awaited in place, an abandoned stream wrote nothing to the
    ledger at all (docs/notes/day-07.md).

    Every other ending is awaited in place, where nothing is cancelling anything
    and the write landing before the response ends is worth having.
    """
    if record.chunks == 0 and record.outcome == "failed":
        settling = meter.discard(reservation)
    else:
        settling = meter.settle(reservation, record.usage)

    if record.outcome != "abandoned":
        await settling
        return

    task = asyncio.ensure_future(settling)
    _PENDING_SETTLEMENTS.add(task)
    task.add_done_callback(_PENDING_SETTLEMENTS.discard)
