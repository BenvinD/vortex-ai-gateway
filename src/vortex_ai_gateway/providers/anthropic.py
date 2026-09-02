"""The Anthropic adapter — where translation is the whole job.

Anthropic's Messages API disagrees with the unified contract on nearly every
structural question, and each disagreement is decided here rather than leaking
outward:

* **System prompts are not messages.** They are hoisted out of ``messages``
  into a top-level ``system`` string, in order, however deep in the
  conversation the caller put them.
* **Tool results are user turns.** OpenAI's ``role="tool"`` message becomes a
  ``tool_result`` block inside the following user turn, and consecutive
  same-role turns are merged, because Anthropic rejects two turns in a row from
  the same speaker.
* **Content is always blocks.** Text, images and tool calls are all blocks;
  ``tool_calls`` on an assistant message becomes a ``tool_use`` block whose
  ``input`` is parsed JSON, not the contract's argument *string*.
* **Temperature tops out at 1.0**, not 2.0, so a higher request is clamped.

What is *not* translated is rejected. A parameter Anthropic has no equivalent
for — ``n``, ``seed``, ``logprobs``, the sampling penalties — raises
:class:`~vortex_ai_gateway.providers.errors.UnsupportedParameterError` rather
than being quietly dropped: a caller who asked for four samples and silently
received one has no way to discover that from a well-formed response.

Response blocks work the other way. Unknown block types (``thinking`` today,
whatever ships next quarter) are skipped rather than rejected — an additive
block costs the caller nothing, while a rejection costs them the whole answer.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar

import httpx
from pydantic import ValidationError

from vortex_ai_gateway.contracts import (
    AssistantMessage,
    AudioContentPart,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceDelta,
    DeveloperMessage,
    FinishReason,
    FunctionCall,
    FunctionCallDelta,
    FunctionDefinition,
    ImageContentPart,
    Message,
    NamedToolChoice,
    PromptTokensDetails,
    ResponseMessage,
    SystemMessage,
    TextContentPart,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    ToolMessage,
    UserMessage,
)
from vortex_ai_gateway.providers.errors import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    TranslationError,
    UnsupportedParameterError,
)
from vortex_ai_gateway.providers.http import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    HttpChatAdapter,
)

#: The Messages API version pinned in the ``anthropic-version`` header.
DEFAULT_API_VERSION = "2023-06-01"

#: ``max_tokens`` is required by Anthropic and optional in the contract, so an
#: unbounded request needs *some* cap. Deliberately generous: this is a ceiling
#: that stops a runaway generation, not a length the caller asked for.
DEFAULT_MAX_TOKENS = 4096

#: Anthropic's sampling range is half of OpenAI's.
MAX_TEMPERATURE = 1.0

#: An ``error`` event arrives mid-stream, long after the ``200`` that would
#: normally have carried the classification, so the vendor's error *type* is
#: what the taxonomy has to be derived from instead of a status code.
STREAM_ERRORS: dict[str, type[ProviderError]] = {
    "invalid_request_error": ProviderBadRequest,
    "authentication_error": ProviderAuthError,
    "permission_error": ProviderAuthError,
    "rate_limit_error": ProviderRateLimited,
    "overloaded_error": ProviderUnavailable,
    "api_error": ProviderUnavailable,
}

#: Why Anthropic stopped, in the contract's vocabulary. ``pause_turn`` ends a
#: long-running turn the client is invited to continue, which is the closest
#: thing Anthropic has to a plain stop.
STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicAdapter(HttpChatAdapter):
    """Serve completions from Anthropic's ``/v1/messages``."""

    provider_name: ClassVar[str] = "anthropic"
    default_base_url: ClassVar[str] = "https://api.anthropic.com"
    chat_path: ClassVar[str] = "/v1/messages"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        models: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        name: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            models=models,
            timeout=timeout,
            connect_timeout=connect_timeout,
            client=client,
            name=name,
        )
        self.api_version = api_version
        self.default_max_tokens = default_max_tokens

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["anthropic-version"] = self.api_version
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # -- Request translation ------------------------------------------------

    def _encode(
        self, request: ChatCompletionRequest, model: str, *, stream: bool
    ) -> dict[str, Any]:
        self._reject_unsupported(request)
        system, messages = self._translate_messages(request.messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.effective_max_tokens or self.default_max_tokens,
            "temperature": min(request.temperature, MAX_TEMPERATURE),
        }
        if system:
            payload["system"] = system
        if request.top_p < 1.0:
            # Anthropic warns against constraining both; 1.0 is the contract's
            # "unset", so sending it would turn a default into an instruction.
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop_sequences"] = (
                [request.stop] if isinstance(request.stop, str) else list(request.stop)
            )
        if request.tools:
            payload["tools"] = [self._translate_tool(tool.function) for tool in request.tools]
            tool_choice = self._translate_tool_choice(request)
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if request.user:
            payload["metadata"] = {"user_id": request.user}
        if stream:
            payload["stream"] = True
        return payload

    def _reject_unsupported(self, request: ChatCompletionRequest) -> None:
        """Refuse the parameters Anthropic cannot honour, naming each one."""
        unsupported: list[tuple[str, bool, str]] = [
            ("n", request.n != 1, "one completion per request; send it again for another sample"),
            ("seed", request.seed is not None, "sampling cannot be pinned"),
            ("logprobs", request.logprobs, "token log probabilities are not returned"),
            (
                "top_logprobs",
                request.top_logprobs is not None,
                "log probabilities are not returned",
            ),
            ("logit_bias", request.logit_bias is not None, "per-token bias cannot be set"),
            ("frequency_penalty", request.frequency_penalty != 0.0, "no repetition penalties"),
            ("presence_penalty", request.presence_penalty != 0.0, "no repetition penalties"),
            ("prediction", request.prediction is not None, "no predicted-output decoding"),
            ("service_tier", request.service_tier is not None, "no per-request latency tier"),
            ("store", bool(request.store), "completions are not retained upstream"),
        ]
        for parameter, offending, detail in unsupported:
            if offending:
                raise UnsupportedParameterError(parameter, provider=self.name, detail=detail)

        if request.response_format is not None and request.response_format.type != "text":
            raise UnsupportedParameterError(
                "response_format",
                provider=self.name,
                detail="structured output is expressed as a tool; declare one in 'tools' "
                "and force it with 'tool_choice'",
            )

    def _translate_messages(
        self, messages: Sequence[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Split the conversation into a system prompt and Anthropic turns."""
        system: list[str] = []
        turns: list[dict[str, Any]] = []

        def add(role: str, blocks: list[dict[str, Any]]) -> None:
            if not blocks:
                # An assistant turn with neither text nor tool calls is legal in
                # the contract and rejected by Anthropic, so it is dropped
                # rather than sent as an empty turn.
                return
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"].extend(blocks)
            else:
                turns.append({"role": role, "content": blocks})

        for message in messages:
            if isinstance(message, SystemMessage | DeveloperMessage):
                system.append(_flatten_text(message.content))
            elif isinstance(message, UserMessage):
                add("user", self._user_blocks(message))
            elif isinstance(message, AssistantMessage):
                add("assistant", self._assistant_blocks(message))
            elif isinstance(message, ToolMessage):
                add(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": _flatten_text(message.content),
                        }
                    ],
                )

        return "\n\n".join(part for part in system if part) or None, turns

    def _user_blocks(self, message: UserMessage) -> list[dict[str, Any]]:
        if isinstance(message.content, str):
            return [{"type": "text", "text": message.content}]

        blocks: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextContentPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageContentPart):
                blocks.append({"type": "image", "source": self._image_source(part.image_url.url)})
            elif isinstance(part, AudioContentPart):
                raise TranslationError(
                    "Anthropic accepts no audio input; send a transcript as text instead.",
                    provider=self.name,
                    code="unsupported_content_part",
                )
        return blocks

    def _image_source(self, url: str) -> dict[str, str]:
        """Turn an OpenAI image reference into an Anthropic image source."""
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            if ";base64" not in header or not data:
                raise TranslationError(
                    "Anthropic accepts only base64 data URLs; "
                    f"{header[:40]!r} is not one of those.",
                    provider=self.name,
                    code="unsupported_image_encoding",
                )
            return {
                "type": "base64",
                "media_type": header.removeprefix("data:").split(";")[0],
                "data": data,
            }
        if url.startswith(("http://", "https://")):
            return {"type": "url", "url": url}
        raise TranslationError(
            f"Unsupported image URL scheme in {url[:40]!r}; send an http(s) or data URL.",
            provider=self.name,
            code="unsupported_image_url",
        )

    def _assistant_blocks(self, message: AssistantMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        text = _flatten_text(message.content) if message.content is not None else ""
        if text:
            blocks.append({"type": "text", "text": text})
        if message.refusal:
            blocks.append({"type": "text", "text": message.refusal})

        for call in message.tool_calls or []:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": self._tool_arguments(call),
                }
            )
        return blocks

    def _tool_arguments(self, call: ToolCall) -> dict[str, Any]:
        """Parse a call's argument string, which Anthropic wants as an object."""
        raw = call.function.arguments.strip() or "{}"
        try:
            arguments = json.loads(raw)
        except ValueError as exc:
            raise TranslationError(
                f"Tool call {call.id!r} carries arguments that are not JSON, and Anthropic "
                f"takes tool input as an object: {raw[:120]!r}",
                provider=self.name,
                code="invalid_tool_arguments",
            ) from exc
        if not isinstance(arguments, dict):
            raise TranslationError(
                f"Tool call {call.id!r} carries {type(arguments).__name__} arguments; "
                "Anthropic takes tool input as an object.",
                provider=self.name,
                code="invalid_tool_arguments",
            )
        return arguments

    @staticmethod
    def _translate_tool(function: FunctionDefinition) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "name": function.name,
            "input_schema": function.parameters or {"type": "object", "properties": {}},
        }
        if function.description:
            tool["description"] = function.description
        return tool

    @staticmethod
    def _translate_tool_choice(request: ChatCompletionRequest) -> dict[str, Any] | None:
        """Map ``tool_choice`` and ``parallel_tool_calls`` onto one object."""
        choice = request.tool_choice
        selection: dict[str, Any] | None
        if isinstance(choice, NamedToolChoice):
            selection = {"type": "tool", "name": choice.function.name}
        elif choice == "required":
            selection = {"type": "any"}
        elif choice == "none":
            selection = {"type": "none"}
        elif choice == "auto" or not request.parallel_tool_calls:
            # With tools declared, Anthropic's default is already "auto"; it is
            # spelled out here only because the parallel-call switch has to hang
            # off something.
            selection = {"type": "auto"}
        else:
            selection = None

        if selection is not None and not request.parallel_tool_calls:
            if selection["type"] in {"auto", "any"}:
                selection["disable_parallel_tool_use"] = True
        return selection

    # -- Response translation -----------------------------------------------

    def _decode(
        self, payload: Any, request: ChatCompletionRequest, model: str
    ) -> ChatCompletionResponse:
        raw = self._require_mapping(payload, "the message")
        content, tool_calls = self._decode_content(raw.get("content"))
        upstream_model = raw.get("model")

        try:
            return ChatCompletionResponse(
                id=str(raw.get("id") or ""),
                model=request.model,
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ResponseMessage(content=content, tool_calls=tool_calls or None),
                        finish_reason=_finish_reason(raw.get("stop_reason")),
                    )
                ],
                usage=_usage(raw.get("usage")),
                vortex=self.metadata(upstream_model if isinstance(upstream_model, str) else model),
            )
        except ValidationError as exc:
            raise self._protocol_error(str(exc)) from exc

    def _decode_content(self, content: Any) -> tuple[str | None, list[ToolCall]]:
        """Fold Anthropic's content blocks into one message and its tool calls."""
        if content is None:
            return None, []
        if isinstance(content, str) or not isinstance(content, Sequence):
            raise self._protocol_error(
                f"expected 'content' to be an array of blocks, got {type(content).__name__}"
            )

        text: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            fragment = self._require_mapping(block, "a content block")
            kind = fragment.get("type")
            if kind == "text":
                text.append(str(fragment.get("text", "")))
            elif kind == "tool_use":
                tool_calls.append(self._decode_tool_use(fragment))
            # Any other block type — 'thinking', or whatever is added next — is
            # skipped: an additive block should not cost the caller the answer.

        return "".join(text) or None, tool_calls

    def _decode_tool_use(self, block: Mapping[str, Any]) -> ToolCall:
        identifier, name = block.get("id"), block.get("name")
        if (
            not isinstance(identifier, str)
            or not isinstance(name, str)
            or not identifier
            or not name
        ):
            raise self._protocol_error(f"a 'tool_use' block is missing its id or name: {block!r}")
        return ToolCall(
            id=identifier,
            function=FunctionCall(name=name, arguments=json.dumps(block.get("input") or {})),
        )

    # -- Streaming ----------------------------------------------------------

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Translate Anthropic's event stream into OpenAI-shaped chunks.

        The two streams are structured differently. Anthropic narrates a
        document being built — blocks open, receive deltas and close, and usage
        arrives at the end — while OpenAI emits a flat run of deltas. The
        bookkeeping that bridges them is the block-index-to-tool-ordinal map:
        Anthropic numbers *every* block, OpenAI numbers only the tool calls.
        """
        model = self.upstream_model(request.model)
        payload = self._encode(request, model, stream=True)

        completion_id = ""
        upstream_model = model
        prompt_usage: Any = None
        output_tokens = 0
        stop_reason: Any = None
        tool_ordinals: dict[int, int] = {}

        def chunk(choices: list[ChatCompletionChunkChoice]) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=completion_id or f"chatcmpl-{self.name}",
                model=request.model,
                choices=choices,
                vortex=self.metadata(upstream_model),
            )

        def delta(fragment: ChoiceDelta) -> ChatCompletionChunk:
            return chunk([ChatCompletionChunkChoice(index=0, delta=fragment)])

        async for data in self._sse_payloads(self._lines(self.chat_path, payload)):
            event = self._require_mapping(self._parse_json(data), "a stream event")
            kind = event.get("type")

            if kind == "message_start":
                message = self._require_mapping(event.get("message"), "'message'")
                completion_id = str(message.get("id") or "")
                if isinstance(message.get("model"), str):
                    upstream_model = str(message["model"])
                prompt_usage = message.get("usage")
                yield delta(ChoiceDelta(role="assistant"))

            elif kind == "content_block_start":
                block = self._require_mapping(event.get("content_block"), "'content_block'")
                if block.get("type") == "tool_use":
                    ordinal = len(tool_ordinals)
                    tool_ordinals[_int(event.get("index"))] = ordinal
                    call = self._decode_tool_use(block)
                    yield delta(
                        ChoiceDelta(
                            tool_calls=[
                                ToolCallDelta(
                                    index=ordinal,
                                    id=call.id,
                                    type="function",
                                    function=FunctionCallDelta(name=call.function.name),
                                )
                            ]
                        )
                    )
                elif block.get("text"):
                    yield delta(ChoiceDelta(content=str(block["text"])))

            elif kind == "content_block_delta":
                body = self._require_mapping(event.get("delta"), "'delta'")
                if body.get("type") == "text_delta":
                    yield delta(ChoiceDelta(content=str(body.get("text", ""))))
                elif body.get("type") == "input_json_delta":
                    yield delta(
                        ChoiceDelta(
                            tool_calls=[
                                ToolCallDelta(
                                    index=tool_ordinals.get(_int(event.get("index")), 0),
                                    function=FunctionCallDelta(
                                        arguments=str(body.get("partial_json", ""))
                                    ),
                                )
                            ]
                        )
                    )

            elif kind == "message_delta":
                body = self._require_mapping(event.get("delta"), "'delta'")
                stop_reason = body.get("stop_reason", stop_reason)
                if isinstance(event.get("usage"), Mapping):
                    output_tokens = _int(event["usage"].get("output_tokens"))

            elif kind == "error":
                detail, code = _describe_stream_error(event.get("error"))
                raise STREAM_ERRORS.get(code or "", ProviderUnavailable)(
                    f"{self.name} failed mid-stream: {detail}",
                    provider=self.name,
                    code=code,
                )

            elif kind == "message_stop":
                yield chunk(
                    [
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChoiceDelta(),
                            finish_reason=_finish_reason(stop_reason),
                        )
                    ]
                )
                if request.stream_options is not None and request.stream_options.include_usage:
                    final = chunk([])
                    final.usage = _usage(prompt_usage, output_tokens)
                    yield final
                break


def _flatten_text(content: str | Sequence[TextContentPart]) -> str:
    """Reduce text-only content to a single string."""
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content)


def _finish_reason(stop_reason: Any) -> FinishReason:
    """Map a stop reason, treating an unknown one as an ordinary stop."""
    return STOP_REASONS.get(str(stop_reason), "stop")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(raw: Any, output_tokens: int | None = None) -> TokenUsage:
    """Convert Anthropic's token counts, which split the prompt three ways.

    ``input_tokens`` excludes anything served from or written to the prompt
    cache, so the three are summed to get the prompt total a caller expects,
    with the cached share preserved in ``prompt_tokens_details``.
    """
    usage: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    cached = _int(usage.get("cache_read_input_tokens"))
    prompt = (
        _int(usage.get("input_tokens")) + cached + _int(usage.get("cache_creation_input_tokens"))
    )
    completion = _int(usage.get("output_tokens")) if output_tokens is None else output_tokens

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=cached) if cached else None,
    )


def _describe_stream_error(error: Any) -> tuple[str, str | None]:
    if isinstance(error, Mapping):
        message = error.get("message")
        code = error.get("type")
        return (
            str(message) if message else "no detail given",
            str(code) if isinstance(code, str) else None,
        )
    return (str(error) if error else "no detail given"), None
