"""The OpenAI adapter — the one whose translation is almost nothing.

The unified contract *is* OpenAI's chat-completions shape (ADR-009), so this
adapter is mostly a transport: serialise the request, POST it, parse the answer
back. That is the payoff of the contract choice, and it makes this file the
reference against which the other two adapters can be read — whatever
:mod:`~vortex_ai_gateway.providers.anthropic` and
:mod:`~vortex_ai_gateway.providers.ollama` have to *do*, they are doing it to
reach the shape that arrives here for free.

Two things are not free. The vendor's model ID is swapped back to the alias the
caller used, so an aliased request gets an answer naming the model it asked
for. And the response is parsed *strictly*: the contract forbids unknown
fields, so a key OpenAI adds tomorrow fails loudly here instead of vanishing on
the way through. That is the deliberate trade of ADR-010, and the fix when it
fires belongs in ``contracts/`` — see ``tests/golden/openai/README.md``.
"""

from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from vortex_ai_gateway.providers.http import HttpChatAdapter

#: OpenAI's terminator for a completed SSE stream.
STREAM_DONE = "[DONE]"


class OpenAIAdapter(HttpChatAdapter):
    """Serve completions from OpenAI's ``/v1/chat/completions``.

    Any API that copies OpenAI's wire format — Azure OpenAI, Together,
    Groq, vLLM, OpenRouter — is reachable by pointing ``base_url`` at it, which
    is why this class carries no vendor-specific quirks beyond the endpoint.
    """

    provider_name: ClassVar[str] = "openai"
    default_base_url: ClassVar[str] = "https://api.openai.com/v1"
    chat_path: ClassVar[str] = "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _encode(
        self, request: ChatCompletionRequest, model: str, *, stream: bool
    ) -> dict[str, Any]:
        """Serialise the request as-is, bar the four fields the gateway owns.

        ``exclude_none`` matters: the contract spells "not set" as ``None``,
        while OpenAI reads an explicit ``null`` as a value for some fields.
        """
        payload = request.model_dump(mode="json", exclude_none=True, by_alias=True)
        payload["model"] = model
        payload["stream"] = stream
        if not stream:
            payload.pop("stream_options", None)
        if "max_completion_tokens" in payload:
            # Both spellings validate to the same cap (see the contract's
            # reconciliation validator); sending the deprecated one alongside
            # its replacement is rejected upstream.
            payload.pop("max_tokens", None)
        return payload

    def _decode(
        self, payload: Any, request: ChatCompletionRequest, model: str
    ) -> ChatCompletionResponse:
        raw = dict(self._require_mapping(payload, "the completion"))
        upstream_model = raw.get("model")
        try:
            response = ChatCompletionResponse.model_validate(raw)
        except ValidationError as exc:
            raise self._protocol_error(str(exc)) from exc

        response.model = request.model
        response.vortex = self.metadata(
            upstream_model if isinstance(upstream_model, str) else model
        )
        return response

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Relay OpenAI's SSE stream, re-badged with the caller's model name."""
        model = self.upstream_model(request.model)
        payload = self._encode(request, model, stream=True)

        async for data in self._sse_payloads(self._lines(self.chat_path, payload)):
            if data == STREAM_DONE:
                break
            yield self._decode_chunk(self._parse_json(data), request, model)

    def _decode_chunk(
        self, payload: Any, request: ChatCompletionRequest, model: str
    ) -> ChatCompletionChunk:
        raw: Mapping[str, Any] = self._require_mapping(payload, "a stream chunk")
        upstream_model = raw.get("model")
        try:
            chunk = ChatCompletionChunk.model_validate(dict(raw))
        except ValidationError as exc:
            raise self._protocol_error(str(exc)) from exc

        chunk.model = request.model
        chunk.vortex = self.metadata(upstream_model if isinstance(upstream_model, str) else model)
        return chunk
