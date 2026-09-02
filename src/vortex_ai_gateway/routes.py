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
"""

from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from vortex_ai_gateway.auth import require_api_key
from vortex_ai_gateway.contracts import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorDetail,
    ErrorResponse,
    ErrorType,
)
from vortex_ai_gateway.providers.base import ChatProvider
from vortex_ai_gateway.providers.errors import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from vortex_ai_gateway.routing import UnroutableModelError

logger = structlog.get_logger(__name__)

#: Terminator every OpenAI-compatible SSE stream ends with. Clients stop
#: reading on it rather than on the connection closing.
SSE_DONE = "data: [DONE]\n\n"

#: How each provider failure is reported. Checked in order, so a subclass may
#: precede its parent; anything unmatched is a ``502``.
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


def create_chat_router(provider: ChatProvider) -> APIRouter:
    """Build the ``/v1`` router served by ``provider``.

    The provider is injected rather than looked up, so a test can mount the same
    routes over a scripted double with no patching.
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
    async def create_chat_completion(request: ChatCompletionRequest) -> Response:
        """Serve a chat completion, streamed or whole.

        A streaming request cannot report a failure with a status code — the
        ``200`` is already on the wire by the time the provider breaks — so the
        two paths diverge here: a ``502`` envelope for the buffered case, an
        error event mid-stream for the other.
        """
        if request.stream:
            return StreamingResponse(
                _stream_events(provider, request),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        try:
            completion = await provider.complete(request)
        except Exception as exc:
            logger.exception("provider request failed", provider=provider.name, model=request.model)
            http_status, envelope = _failure(exc)
            retry_after = getattr(exc, "retry_after", None)
            return JSONResponse(
                status_code=http_status,
                content=envelope.model_dump(mode="json"),
                headers={"Retry-After": str(int(retry_after))} if retry_after else None,
            )

        return JSONResponse(content=completion.model_dump(mode="json", exclude_none=True))

    return router


async def _stream_events(
    provider: ChatProvider, request: ChatCompletionRequest
) -> AsyncIterator[str]:
    """Yield the SSE body for a streaming completion.

    A failure part-way through is emitted as one final error event: the status
    line is long gone, so this is the only way the client learns the stream was
    truncated rather than finished.
    """
    try:
        async for chunk in provider.stream(request):
            yield _sse(chunk.model_dump_json(exclude_none=True))
    except Exception as exc:
        logger.exception("provider stream failed", provider=provider.name, model=request.model)
        yield _sse(_failure(exc)[1].model_dump_json())

    yield SSE_DONE
