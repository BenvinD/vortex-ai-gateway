"""The Ollama adapter — a local runtime with a nearly-OpenAI shape.

Ollama's ``/api/chat`` is close enough to the contract that the message
translation is short, and different enough in three specific ways that the
differences are worth naming:

* **Sampling lives in ``options``.** ``temperature``, ``seed``, ``stop`` and
  the output cap (``num_predict``) are nested rather than top-level, and are
  sent only when the caller set them — an option present in the body overrides
  whatever the model's Modelfile chose.
* **Tool calls have no IDs.** Ollama returns a function name and an argument
  *object*; the contract requires an ID and an argument *string*. Both are
  synthesised here, deterministically, so a caller can send the matching
  ``tool`` message straight back.
* **Streaming is JSON Lines, not SSE.** Every line is a whole message object,
  and the last one carries the token counts.

There is no API key: an Ollama instance is normally reached over the loopback
address or a private network. ``api_key`` is still accepted, and sent as a
bearer token, because the hosted and proxied deployments do want one.
"""

import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar
from uuid import uuid4

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
    ImageContentPart,
    JsonSchemaResponseFormat,
    Message,
    ResponseMessage,
    SystemMessage,
    TextContentPart,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    ToolMessage,
    UserMessage,
)
from vortex_ai_gateway.providers.errors import TranslationError, UnsupportedParameterError
from vortex_ai_gateway.providers.http import HttpChatAdapter

#: Why Ollama stopped. It reports far fewer reasons than the contract carries;
#: an unrecognised one is treated as an ordinary stop.
DONE_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "load": "stop",
    "unload": "stop",
}


class OllamaAdapter(HttpChatAdapter):
    """Serve completions from a local (or self-hosted) Ollama runtime."""

    provider_name: ClassVar[str] = "ollama"
    default_base_url: ClassVar[str] = "http://localhost:11434"
    chat_path: ClassVar[str] = "/api/chat"
    requires_api_key: ClassVar[bool] = False

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    # -- Request translation ------------------------------------------------

    def _encode(
        self, request: ChatCompletionRequest, model: str, *, stream: bool
    ) -> dict[str, Any]:
        self._reject_unsupported(request)

        payload: dict[str, Any] = {
            "model": model,
            "messages": self._translate_messages(request.messages),
            "stream": stream,
        }
        options = _options(request)
        if options:
            payload["options"] = options
        if request.tools:
            payload["tools"] = [
                tool.model_dump(mode="json", exclude_none=True, by_alias=True)
                for tool in request.tools
            ]
        response_format = _response_format(request)
        if response_format is not None:
            payload["format"] = response_format
        return payload

    def _reject_unsupported(self, request: ChatCompletionRequest) -> None:
        """Refuse the parameters Ollama cannot honour, naming each one."""
        unsupported: list[tuple[str, bool, str]] = [
            ("n", request.n != 1, "one completion per request; send it again for another sample"),
            ("logprobs", request.logprobs, "token log probabilities are not returned"),
            (
                "top_logprobs",
                request.top_logprobs is not None,
                "log probabilities are not returned",
            ),
            ("logit_bias", request.logit_bias is not None, "per-token bias cannot be set"),
            ("prediction", request.prediction is not None, "no predicted-output decoding"),
            ("service_tier", request.service_tier is not None, "no per-request latency tier"),
            ("store", bool(request.store), "completions are not retained upstream"),
            (
                "tool_choice",
                request.tool_choice not in (None, "auto"),
                "the model decides whether to call a tool; it cannot be forced or forbidden",
            ),
        ]
        for parameter, offending, detail in unsupported:
            if offending:
                raise UnsupportedParameterError(parameter, provider=self.name, detail=detail)

    def _translate_messages(self, messages: Sequence[Message]) -> list[dict[str, Any]]:
        """Flatten the conversation into Ollama's message list.

        Tool results carry ``tool_name`` as well as the content: Ollama matches
        a result to its call by name, having never issued an ID to match on.
        """
        called_names: dict[str, str] = {}
        translated: list[dict[str, Any]] = []

        for message in messages:
            if isinstance(message, SystemMessage | DeveloperMessage):
                # 'developer' is OpenAI's newer spelling of 'system'; Ollama
                # knows only the older one.
                translated.append({"role": "system", "content": _flatten_text(message.content)})

            elif isinstance(message, UserMessage):
                translated.append(self._user_message(message))

            elif isinstance(message, AssistantMessage):
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": _flatten_text(message.content) if message.content else "",
                }
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "function": {
                                "name": call.function.name,
                                "arguments": self._tool_arguments(call),
                            }
                        }
                        for call in message.tool_calls
                    ]
                    called_names.update(
                        {call.id: call.function.name for call in message.tool_calls}
                    )
                translated.append(entry)

            elif isinstance(message, ToolMessage):
                result: dict[str, Any] = {
                    "role": "tool",
                    "content": _flatten_text(message.content),
                }
                name = called_names.get(message.tool_call_id)
                if name is not None:
                    result["tool_name"] = name
                translated.append(result)

        return translated

    def _user_message(self, message: UserMessage) -> dict[str, Any]:
        """Split a user turn into text plus Ollama's flat list of images."""
        if isinstance(message.content, str):
            return {"role": "user", "content": message.content}

        text: list[str] = []
        images: list[str] = []
        for part in message.content:
            if isinstance(part, TextContentPart):
                text.append(part.text)
            elif isinstance(part, ImageContentPart):
                images.append(self._image_data(part.image_url.url))
            elif isinstance(part, AudioContentPart):
                raise TranslationError(
                    "Ollama's chat API accepts no audio input; send a transcript as text instead.",
                    provider=self.name,
                    code="unsupported_content_part",
                )

        entry: dict[str, Any] = {"role": "user", "content": "\n".join(text)}
        if images:
            entry["images"] = images
        return entry

    def _image_data(self, url: str) -> str:
        """Extract the base64 payload Ollama wants from an image reference."""
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            if ";base64" not in header or not data:
                raise TranslationError(
                    f"Ollama takes images as base64; {header[:40]!r} is not a base64 data URL.",
                    provider=self.name,
                    code="unsupported_image_encoding",
                )
            return data
        raise TranslationError(
            f"Ollama does not fetch images by URL ({url[:40]!r}); inline it as a base64 data URL.",
            provider=self.name,
            code="unsupported_image_url",
        )

    def _tool_arguments(self, call: ToolCall) -> dict[str, Any]:
        """Parse a call's argument string, which Ollama wants as an object."""
        raw = call.function.arguments.strip() or "{}"
        try:
            arguments = json.loads(raw)
        except ValueError as exc:
            raise TranslationError(
                f"Tool call {call.id!r} carries arguments that are not JSON, and Ollama "
                f"takes tool input as an object: {raw[:120]!r}",
                provider=self.name,
                code="invalid_tool_arguments",
            ) from exc
        if not isinstance(arguments, dict):
            raise TranslationError(
                f"Tool call {call.id!r} carries {type(arguments).__name__} arguments; "
                "Ollama takes tool input as an object.",
                provider=self.name,
                code="invalid_tool_arguments",
            )
        return arguments

    # -- Response translation -----------------------------------------------

    def _decode(
        self, payload: Any, request: ChatCompletionRequest, model: str
    ) -> ChatCompletionResponse:
        raw = self._require_mapping(payload, "the completion")
        message = self._require_mapping(raw.get("message", {}), "'message'")
        tool_calls = self._decode_tool_calls(message.get("tool_calls"))
        upstream_model = raw.get("model")

        try:
            return ChatCompletionResponse(
                id=f"chatcmpl-{uuid4().hex}",
                created=_created(raw.get("created_at")),
                model=request.model,
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=ResponseMessage(
                            content=str(message.get("content") or "") or None,
                            tool_calls=tool_calls or None,
                        ),
                        finish_reason=_finish_reason(raw.get("done_reason"), tool_calls),
                    )
                ],
                usage=_usage(raw),
                vortex=self.metadata(upstream_model if isinstance(upstream_model, str) else model),
            )
        except ValidationError as exc:
            raise self._protocol_error(str(exc)) from exc

    def _decode_tool_calls(self, raw: Any, offset: int = 0) -> list[ToolCall]:
        """Read a batch of calls, numbering them from ``offset``.

        A stream can deliver calls over several lines, and the synthesised IDs
        have to stay unique across the whole answer, not just one line.
        """
        if raw is None:
            return []
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            raise self._protocol_error(
                f"expected 'tool_calls' to be an array, got {type(raw).__name__}"
            )

        calls: list[ToolCall] = []
        for position, entry in enumerate(raw):
            function = self._require_mapping(
                self._require_mapping(entry, "a tool call").get("function", {}), "'function'"
            )
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise self._protocol_error(f"a tool call is missing its function name: {entry!r}")
            calls.append(
                ToolCall(
                    id=_tool_call_id(offset + position, name),
                    function=FunctionCall(
                        name=name, arguments=json.dumps(function.get("arguments") or {})
                    ),
                )
            )
        return calls

    # -- Streaming ----------------------------------------------------------

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Translate Ollama's JSON Lines stream into OpenAI-shaped chunks.

        Ollama repeats the whole message envelope on every line and marks the
        end with ``done``; the deltas are the ``message.content`` fragments in
        between. A tool call arrives whole rather than in pieces, and is still
        split into a name delta and an argument delta, because that is what a
        client parsing an OpenAI stream is written to reassemble.
        """
        model = self.upstream_model(request.model)
        payload = self._encode(request, model, stream=True)

        completion_id = f"chatcmpl-{uuid4().hex}"
        upstream_model = model
        started = False
        # Tool calls can arrive on an earlier line than the one carrying
        # ``done``, so the finish reason is decided from the whole answer.
        calls_so_far: list[ToolCall] = []

        def chunk(choices: list[ChatCompletionChunkChoice]) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=completion_id,
                model=request.model,
                choices=choices,
                vortex=self.metadata(upstream_model),
            )

        def delta(fragment: ChoiceDelta) -> ChatCompletionChunk:
            return chunk([ChatCompletionChunkChoice(index=0, delta=fragment)])

        async for line in self._lines(self.chat_path, payload):
            if not line.strip():
                continue
            event = self._require_mapping(self._parse_json(line), "a stream line")
            message = self._require_mapping(event.get("message", {}), "'message'")

            if not started:
                started = True
                if isinstance(event.get("model"), str):
                    upstream_model = str(event["model"])
                yield delta(ChoiceDelta(role="assistant"))

            content = message.get("content")
            if content:
                yield delta(ChoiceDelta(content=str(content)))

            tool_calls = self._decode_tool_calls(message.get("tool_calls"), len(calls_so_far))
            for position, call in enumerate(tool_calls, start=len(calls_so_far)):
                yield delta(
                    ChoiceDelta(
                        tool_calls=[
                            ToolCallDelta(
                                index=position,
                                id=call.id,
                                type="function",
                                function=FunctionCallDelta(name=call.function.name),
                            )
                        ]
                    )
                )
                yield delta(
                    ChoiceDelta(
                        tool_calls=[
                            ToolCallDelta(
                                index=position,
                                function=FunctionCallDelta(arguments=call.function.arguments),
                            )
                        ]
                    )
                )
            calls_so_far.extend(tool_calls)

            if event.get("done"):
                yield chunk(
                    [
                        ChatCompletionChunkChoice(
                            index=0,
                            delta=ChoiceDelta(),
                            finish_reason=_finish_reason(event.get("done_reason"), calls_so_far),
                        )
                    ]
                )
                if request.stream_options is not None and request.stream_options.include_usage:
                    final = chunk([])
                    final.usage = _usage(event)
                    yield final
                break


def _flatten_text(content: str | Sequence[TextContentPart]) -> str:
    """Reduce text-only content to a single string."""
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content)


def _options(request: ChatCompletionRequest) -> dict[str, Any]:
    """Collect the sampling settings the caller actually asked for.

    Only non-default values are sent. An option present in the request body
    overrides the model's own Modelfile setting, so forwarding the contract's
    defaults would silently overwrite whatever the operator configured.
    """
    options: dict[str, Any] = {}
    if request.temperature != 1.0:
        options["temperature"] = request.temperature
    if request.top_p < 1.0:
        options["top_p"] = request.top_p
    if request.seed is not None:
        options["seed"] = request.seed
    if request.frequency_penalty != 0.0:
        options["frequency_penalty"] = request.frequency_penalty
    if request.presence_penalty != 0.0:
        options["presence_penalty"] = request.presence_penalty
    if request.stop is not None:
        options["stop"] = [request.stop] if isinstance(request.stop, str) else list(request.stop)
    if request.effective_max_tokens is not None:
        options["num_predict"] = request.effective_max_tokens
    return options


def _response_format(request: ChatCompletionRequest) -> str | dict[str, Any] | None:
    """Map ``response_format`` onto Ollama's ``format``.

    Ollama takes either the literal ``"json"`` or a JSON Schema object, which
    covers both of the contract's structured modes exactly.
    """
    fmt = request.response_format
    if fmt is None or fmt.type == "text":
        return None
    if isinstance(fmt, JsonSchemaResponseFormat):
        return fmt.json_schema.json_schema
    return "json"


def _tool_call_id(position: int, name: str) -> str:
    """Invent the ID Ollama never issued.

    Derived from the call's position and name so it is stable across a retry of
    the same response, and readable in a log line.
    """
    return f"call_{position}_{name}"


def _finish_reason(done_reason: Any, tool_calls: Sequence[ToolCall]) -> FinishReason:
    if tool_calls and done_reason in (None, "stop"):
        return "tool_calls"
    return DONE_REASONS.get(str(done_reason), "stop")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _created(created_at: Any) -> int:
    """Convert Ollama's RFC 3339 timestamp to the epoch seconds OpenAI reports.

    Ollama's own clock is preferred over ours so the timestamp describes when
    the answer was produced; an unparseable one falls back to now rather than
    to zero, which would read as 1970 in every client that renders it.
    """
    if isinstance(created_at, str) and created_at:
        try:
            return int(datetime.fromisoformat(created_at).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _usage(raw: Mapping[str, Any]) -> TokenUsage:
    prompt = _int(raw.get("prompt_eval_count"))
    completion = _int(raw.get("eval_count"))
    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )
