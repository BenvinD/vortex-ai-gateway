"""The OpenAI-compatible HTTP surface.

The router is thin on purpose. It owns three things a provider must not: the
JSON/SSE framing, the mapping from an upstream failure to an HTTP status, and
the decision to stream. Everything else is delegated — validation to the
contract models, generation to the injected
:class:`~vortex_ai_gateway.providers.base.ChatProvider`.
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
)
from vortex_ai_gateway.providers.base import ChatProvider

logger = structlog.get_logger(__name__)

#: Terminator every OpenAI-compatible SSE stream ends with. Clients stop
#: reading on it rather than on the connection closing.
SSE_DONE = "data: [DONE]\n\n"


def _sse(payload: str) -> str:
    """Frame one JSON document as a server-sent event."""
    return f"data: {payload}\n\n"


def _upstream_failure(exc: Exception) -> ErrorResponse:
    """Describe a provider failure without leaking its internals to the caller."""
    return ErrorResponse(
        error=ErrorDetail(
            message=f"The upstream provider failed to serve this request: {exc}",
            type="api_error",
            code=type(exc).__name__,
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
            502: {"model": ErrorResponse},
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
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content=_upstream_failure(exc).model_dump(mode="json"),
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
        yield _sse(_upstream_failure(exc).model_dump_json())

    yield SSE_DONE
