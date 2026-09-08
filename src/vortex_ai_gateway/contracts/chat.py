"""The chat-completions request and response contract.

This is the gateway's front door. A caller sends a
:class:`ChatCompletionRequest`; whichever provider serves it, the caller gets
a :class:`ChatCompletionResponse` (or a stream of
:class:`ChatCompletionChunk`). The shapes mirror OpenAI's
``/v1/chat/completions`` so existing clients work unchanged, with one additive
extension — :attr:`ChatCompletionResponse.vortex` — that says who actually
served the call.
"""

import time
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from vortex_ai_gateway.contracts.base import ContractModel, NonEmptyStr, UnixTimestamp
from vortex_ai_gateway.contracts.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
)
from vortex_ai_gateway.contracts.options import (
    PredictedOutput,
    ResponseFormat,
    StreamOptions,
)
from vortex_ai_gateway.contracts.tools import NamedToolChoice, ToolChoice, ToolDefinition
from vortex_ai_gateway.contracts.usage import ChoiceLogprobs, TokenUsage

#: Why generation stopped. ``tool_calls`` means the model wants a tool run and
#: the conversation is expected to continue.
FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]

#: Latency/cost tier requested of the upstream, where it offers one.
ServiceTier = Literal["auto", "default", "flex", "scale"]

#: A token bias, keyed by token ID as a string, as OpenAI defines it.
LogitBias = dict[
    Annotated[str, Field(pattern=r"^-?\d+$")],
    Annotated[float, Field(ge=-100.0, le=100.0)],
]

_MAX_STOP_SEQUENCES = 4
_MAX_METADATA_ENTRIES = 16


def _new_completion_id() -> str:
    """Mint a response ID in OpenAI's ``chatcmpl-`` shape."""
    return f"chatcmpl-{uuid4().hex}"


def _now() -> int:
    """Current Unix time in seconds, as responses report it."""
    return int(time.time())


class ChatCompletionRequest(ContractModel):
    """Everything a caller may ask for, validated before any provider is picked.

    Validation happens here, once, in provider-neutral terms. An adapter
    downstream may still narrow this (a provider without ``logit_bias`` has to
    reject or drop it), but no adapter should have to re-check that
    ``temperature`` is a number in range.
    """

    model: NonEmptyStr = Field(
        description="Model or routing alias to serve this request.",
    )
    messages: Annotated[list[Message], Field(min_length=1)] = Field(
        description="Conversation so far, oldest first.",
    )

    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    n: int = Field(default=1, ge=1, le=128, description="How many choices to generate.")
    seed: int | None = Field(
        default=None,
        description="Best-effort determinism; providers may ignore it.",
    )
    stop: (
        NonEmptyStr | Annotated[list[NonEmptyStr], Field(max_length=_MAX_STOP_SEQUENCES)] | None
    ) = None

    max_completion_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Deprecated by OpenAI in favour of 'max_completion_tokens'; still accepted.",
        # Marked deprecated in the generated OpenAPI schema only. Field(deprecated=...)
        # would also warn on every internal read, including the ones below.
        json_schema_extra={"deprecated": True},
    )

    logprobs: bool = False
    top_logprobs: int | None = Field(default=None, ge=0, le=20)
    logit_bias: LogitBias | None = None

    response_format: ResponseFormat | None = None
    prediction: PredictedOutput | None = None

    stream: bool = False
    stream_options: StreamOptions | None = None

    tools: Annotated[list[ToolDefinition], Field(min_length=1)] | None = None
    tool_choice: ToolChoice | None = None
    parallel_tool_calls: bool = True

    service_tier: ServiceTier | None = None
    store: bool | None = Field(
        default=None,
        description="Ask the upstream to retain this exchange for its own tooling.",
    )
    metadata: dict[str, str] | None = Field(
        default=None,
        max_length=_MAX_METADATA_ENTRIES,
        description="Caller-defined tags echoed through logging and billing.",
    )
    user: str | None = Field(
        default=None,
        description="Stable end-user identifier, used for abuse tracing and shard affinity.",
    )

    @property
    def effective_max_tokens(self) -> int | None:
        """The output cap to enforce, whichever spelling the caller used."""
        if self.max_completion_tokens is not None:
            return self.max_completion_tokens
        return self.max_tokens

    @model_validator(mode="after")
    def _reconcile_token_limits(self) -> Self:
        """Reject two different answers to "how long may the output be?"."""
        both_set = self.max_completion_tokens is not None and self.max_tokens is not None
        if both_set and self.max_completion_tokens != self.max_tokens:
            raise ValueError(
                "'max_tokens' and 'max_completion_tokens' disagree; send only "
                "'max_completion_tokens'"
            )
        return self

    @model_validator(mode="after")
    def _check_logprobs_pairing(self) -> Self:
        """``top_logprobs`` is meaningless unless ``logprobs`` is on."""
        if self.top_logprobs is not None and not self.logprobs:
            raise ValueError("'top_logprobs' requires 'logprobs' to be true")
        return self

    @model_validator(mode="after")
    def _check_stream_options_pairing(self) -> Self:
        """``stream_options`` only has an effect on a streaming request."""
        if self.stream_options is not None and not self.stream:
            raise ValueError("'stream_options' requires 'stream' to be true")
        return self

    @model_validator(mode="after")
    def _check_tool_declarations(self) -> Self:
        """Tool names must be unique, and a forced choice must be declared.

        Duplicate names make the model's chosen call ambiguous, and forcing a
        tool that was never offered is a guaranteed upstream 400 — both are
        cheaper to catch here than after a round trip.
        """
        if self.tools is not None:
            names = [tool.function.name for tool in self.tools]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise ValueError(f"duplicate tool names: {', '.join(duplicates)}")

        if isinstance(self.tool_choice, NamedToolChoice):
            declared = {tool.function.name for tool in self.tools or []}
            if self.tool_choice.function.name not in declared:
                raise ValueError(
                    f"'tool_choice' names undeclared tool "
                    f"{self.tool_choice.function.name!r}; add it to 'tools'"
                )
        return self

    @model_validator(mode="after")
    def _check_tool_results_have_calls(self) -> Self:
        """Every tool result must answer a tool call the assistant actually made.

        A dangling ``tool_call_id`` is the classic history-assembly bug, and
        providers reject it with an opaque error. Naming the offending ID here
        makes it a one-line fix for the caller.
        """
        answered: set[str] = set()
        for message in self.messages:
            if isinstance(message, ToolMessage):
                if message.tool_call_id not in answered:
                    raise ValueError(
                        f"tool message references unknown tool_call_id "
                        f"{message.tool_call_id!r}; it must follow an assistant "
                        f"message containing that call"
                    )
            elif isinstance(message, AssistantMessage):
                answered.update(call.id for call in message.tool_calls or [])
        return self


class UrlCitation(ContractModel):
    """A source the model cited, with the span of text it supports."""

    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    title: str
    url: str


class MessageAnnotation(ContractModel):
    """Provenance attached to a span of the assistant's content."""

    type: Literal["url_citation"] = "url_citation"
    url_citation: UrlCitation


class ResponseMessage(ContractModel):
    """The assistant turn the gateway produces."""

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[ToolCall] | None = None
    annotations: list[MessageAnnotation] | None = None


class ChatCompletionChoice(ContractModel):
    """One generated alternative. There are ``n`` of these."""

    index: int = Field(ge=0)
    message: ResponseMessage
    finish_reason: FinishReason
    logprobs: ChoiceLogprobs | None = None


class GatewayMetadata(ContractModel):
    """Gateway-added provenance, absent from OpenAI's schema.

    An additive object under one key: OpenAI clients ignore fields they do not
    know, so this stays compatible while making "which provider answered, and
    under what upstream model name?" answerable from the response alone.
    """

    provider: NonEmptyStr = Field(description="Adapter that served the request, e.g. 'anthropic'.")
    upstream_model: NonEmptyStr = Field(description="Model ID as the provider names it.")
    request_id: str | None = Field(
        default=None,
        description="Correlates with the 'request_id' field in the gateway's logs.",
    )


class ChatCompletionResponse(ContractModel):
    """A completed, non-streamed response in the unified shape."""

    id: str = Field(default_factory=_new_completion_id)
    object: Literal["chat.completion"] = "chat.completion"
    created: UnixTimestamp = Field(default_factory=_now)
    model: NonEmptyStr
    choices: Annotated[list[ChatCompletionChoice], Field(min_length=1)]
    usage: TokenUsage | None = None
    system_fingerprint: str | None = None
    service_tier: ServiceTier | None = None
    vortex: GatewayMetadata | None = None


class FunctionCallDelta(ContractModel):
    """The slice of a function call carried by one streamed chunk."""

    name: str | None = None
    arguments: str | None = None


class ToolCallDelta(ContractModel):
    """A partial tool call. ``index`` identifies which call is being extended."""

    index: int = Field(ge=0)
    id: str | None = None
    type: Literal["function"] | None = None
    function: FunctionCallDelta | None = None


class ChoiceDelta(ContractModel):
    """The incremental part of a choice: fields absent mean "unchanged"."""

    role: Literal["assistant"] | None = None
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[ToolCallDelta] | None = None


class ChatCompletionChunkChoice(ContractModel):
    """One choice's slice of a streamed response."""

    index: int = Field(ge=0)
    delta: ChoiceDelta
    finish_reason: FinishReason | None = None
    logprobs: ChoiceLogprobs | None = None


class ChatCompletionChunk(ContractModel):
    """One server-sent event in a streamed completion.

    ``usage`` is populated only on the final chunk, and only when the request
    asked for it via ``stream_options.include_usage``.
    """

    id: str = Field(default_factory=_new_completion_id)
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: UnixTimestamp = Field(default_factory=_now)
    model: NonEmptyStr
    choices: list[ChatCompletionChunkChoice]
    usage: TokenUsage | None = None
    system_fingerprint: str | None = None
    service_tier: ServiceTier | None = None
    vortex: GatewayMetadata | None = None
