"""The seam every model provider plugs into.

A provider takes a :class:`~vortex_ai_gateway.contracts.ChatCompletionRequest`
in the unified vocabulary and answers in it too. Translating to and from a
vendor's own wire format happens *inside* an implementation; nothing
provider-shaped crosses this boundary.

The protocol is deliberately two methods wide. It is the contract
:class:`~vortex_ai_gateway.providers.mock.MockProvider` and every vendor
adapter satisfy, and it will widen (model listing, embeddings, health) only
when something above it needs those.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)


@runtime_checkable
class ChatProvider(Protocol):
    """Something that can serve a chat completion.

    Implementations raise on failure rather than returning an error envelope:
    mapping an exception to :class:`~vortex_ai_gateway.contracts.ErrorResponse`
    is the routing layer's job, so a provider never has to know which HTTP
    status its caller will use.
    """

    @property
    def name(self) -> str:
        """Short identifier reported in ``ChatCompletionResponse.vortex``."""
        ...

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Serve ``request`` in full and return the finished completion."""
        ...

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Serve ``request`` incrementally, yielding chunks in order.

        The final chunk carries ``finish_reason``. When the request asked for
        ``stream_options.include_usage``, one further chunk follows it with no
        choices and the token usage attached, as OpenAI does.
        """
        ...
