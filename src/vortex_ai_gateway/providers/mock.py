"""A provider that answers from a script instead of an upstream API.

`MockProvider` implements the whole :class:`~vortex_ai_gateway.providers.base.ChatProvider`
surface with no network and no key, so routing, streaming, retries, guardrails
and error mapping can all be exercised at zero cost and in constant time.

It is deliberately more than a stub. Given no script it *derives* a reply from
the request — honouring ``n``, the output cap, ``tool_choice`` and
``response_format`` — so a test that cares about none of those still gets a
response consistent with what it asked for. Given a script it replays it
exactly, and an ``Exception`` in that script is raised rather than returned,
which is how failure paths (retry, breaker, error envelope) get tested.

Everything is deterministic: IDs count up from a fixed prefix, ``created`` is a
frozen timestamp, and token counts are whitespace-delimited words. Responses can
therefore be compared field for field without freezing the clock.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from vortex_ai_gateway.contracts import (
    AssistantMessage,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceDelta,
    FinishReason,
    FunctionCall,
    FunctionCallDelta,
    GatewayMetadata,
    Message,
    NamedToolChoice,
    ResponseMessage,
    TextContentPart,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
)

#: A fixed ``created`` timestamp (2023-11-14T22:13:20Z). Frozen so a response
#: can be asserted whole, rather than field by field around a moving clock.
MOCK_CREATED = 1_700_000_000

#: Splits text into tokens that still carry their trailing whitespace, so the
#: streamed deltas reassemble into exactly the original content.
_STREAM_TOKEN = re.compile(r"\S+\s*")


@dataclass(frozen=True)
class CannedReply:
    """One scripted assistant turn.

    ``finish_reason`` is inferred when left out — ``tool_calls`` if the reply
    calls tools, otherwise ``stop`` — so the common case stays a one-liner:
    ``CannedReply("hello")``.
    """

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    refusal: str | None = None
    finish_reason: FinishReason | None = None

    def resolved_finish_reason(self) -> FinishReason:
        """The reason to report, filling in the obvious one when unset."""
        if self.finish_reason is not None:
            return self.finish_reason
        return "tool_calls" if self.tool_calls else "stop"


#: A scripted step: either a reply to return or an error to raise.
ScriptedOutcome = CannedReply | Exception


def _part_text(content: str | Sequence[object] | None) -> str:
    """Flatten message content down to the text a token count can see."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if isinstance(part, TextContentPart))


def _count_tokens(text: str) -> int:
    """Count tokens as whitespace-delimited words.

    Not a real tokenizer, and not trying to be: a test asserting
    ``prompt_tokens == 3`` should be able to see why by reading the prompt.
    """
    return len(text.split())


def _last_user_text(messages: Sequence[Message]) -> str:
    """The most recent user turn, which is what a derived reply echoes."""
    for message in reversed(messages):
        if message.role == "user":
            return _part_text(message.content)
    return ""


def _prompt_tokens(messages: Sequence[Message]) -> int:
    """Total tokens across the conversation, tool calls included."""
    total = 0
    for message in messages:
        total += _count_tokens(_part_text(message.content))
        if isinstance(message, AssistantMessage) and message.tool_calls:
            for call in message.tool_calls:
                total += _count_tokens(f"{call.function.name} {call.function.arguments}")
    return total


def _reply_tokens(reply: CannedReply) -> int:
    """Tokens the assistant turn is billed for."""
    total = _count_tokens(reply.content or "") + _count_tokens(reply.refusal or "")
    for call in reply.tool_calls:
        total += _count_tokens(f"{call.function.name} {call.function.arguments}")
    return total


@dataclass
class MockProvider:
    """A scripted :class:`ChatProvider` for tests and local development.

    ``replies`` is replayed in order; the last entry repeats once the script is
    exhausted, so a test that scripts one reply and then loops does not fall off
    the end. With no script at all, replies are derived from each request.

    Every request is recorded on :attr:`received_requests`, which is usually the
    real assertion: not just *what* came back, but what the gateway decided to
    send.
    """

    replies: Sequence[ScriptedOutcome] = ()
    name: str = "mock"
    latency_seconds: float = 0.0
    received_requests: list[ChatCompletionRequest] = field(default_factory=list)
    _calls: int = field(default=0, init=False)

    @property
    def call_count(self) -> int:
        """How many requests this provider has been given."""
        return self._calls

    def reset(self) -> None:
        """Rewind the script and forget recorded requests."""
        self._calls = 0
        self.received_requests.clear()

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """Answer ``request`` in one shot."""
        reply, completion_id = await self._accept(request)
        message, finish_reason, completion_tokens = self._render(reply, request)

        return ChatCompletionResponse(
            id=completion_id,
            created=MOCK_CREATED,
            model=request.model,
            choices=[
                ChatCompletionChoice(index=index, message=message, finish_reason=finish_reason)
                for index in range(request.n)
            ],
            usage=self._usage(request, completion_tokens),
            vortex=self._metadata(request),
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        """Answer ``request`` as a sequence of chunks.

        The shape follows OpenAI's: an opening chunk carrying only the role, one
        chunk per token, a chunk carrying ``finish_reason``, and — when
        ``stream_options.include_usage`` was set — a final chunk with no choices
        and the usage attached.
        """
        reply, completion_id = await self._accept(request)
        message, finish_reason, completion_tokens = self._render(reply, request)
        indexes = range(request.n)

        def chunk(choices: list[ChatCompletionChunkChoice]) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=completion_id,
                created=MOCK_CREATED,
                model=request.model,
                choices=choices,
                vortex=self._metadata(request),
            )

        yield chunk(
            [
                ChatCompletionChunkChoice(index=index, delta=ChoiceDelta(role="assistant"))
                for index in indexes
            ]
        )

        for delta in self._deltas(message):
            for index in indexes:
                yield chunk([ChatCompletionChunkChoice(index=index, delta=delta)])

        yield chunk(
            [
                ChatCompletionChunkChoice(
                    index=index, delta=ChoiceDelta(), finish_reason=finish_reason
                )
                for index in indexes
            ]
        )

        if request.stream_options is not None and request.stream_options.include_usage:
            usage_chunk = chunk([])
            usage_chunk.usage = self._usage(request, completion_tokens)
            yield usage_chunk

    async def _accept(self, request: ChatCompletionRequest) -> tuple[CannedReply | None, str]:
        """Record the request, apply the script, and mint this call's ID.

        Returns ``None`` for the reply when there is no script left to apply,
        which tells :meth:`_render` to derive one from the request.
        """
        self.received_requests.append(request)
        self._calls += 1
        completion_id = f"chatcmpl-mock-{self._calls:03d}"

        if self.latency_seconds > 0:
            await asyncio.sleep(self.latency_seconds)

        outcome = self._scripted_outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, completion_id

    def _scripted_outcome(self) -> ScriptedOutcome | None:
        """The script's entry for this call, or ``None`` to derive one."""
        if not self.replies:
            return None
        return self.replies[min(self._calls - 1, len(self.replies) - 1)]

    def _render(
        self, reply: CannedReply | None, request: ChatCompletionRequest
    ) -> tuple[ResponseMessage, FinishReason, int]:
        """Turn a reply (or no reply) into the message this request earns.

        The output cap is applied here rather than in the script, so the same
        canned reply truncates correctly whatever ``max_completion_tokens`` the
        caller sent.
        """
        resolved = reply if reply is not None else self._derive_reply(request)
        content, truncated = self._apply_output_cap(resolved.content, request)
        finish_reason: FinishReason = "length" if truncated else resolved.resolved_finish_reason()

        message = ResponseMessage(
            content=content,
            refusal=resolved.refusal,
            tool_calls=list(resolved.tool_calls) or None,
        )
        billed = CannedReply(
            content=content, refusal=resolved.refusal, tool_calls=resolved.tool_calls
        )
        return message, finish_reason, _reply_tokens(billed)

    def _derive_reply(self, request: ChatCompletionRequest) -> CannedReply:
        """Invent a reply that is consistent with what the request asked for."""
        forced = self._forced_tool_name(request)
        if forced is not None:
            return CannedReply(
                tool_calls=(
                    ToolCall(
                        id=f"call_mock_{self._calls:03d}",
                        function=FunctionCall(name=forced, arguments="{}"),
                    ),
                )
            )

        prompt = _last_user_text(request.messages)
        if request.response_format is not None and request.response_format.type in {
            "json_object",
            "json_schema",
        }:
            return CannedReply(content=json.dumps({"echo": prompt}))
        return CannedReply(content=f"mock reply to: {prompt}" if prompt else "mock reply")

    @staticmethod
    def _forced_tool_name(request: ChatCompletionRequest) -> str | None:
        """The tool this request insists on, if it insists on one."""
        if isinstance(request.tool_choice, NamedToolChoice):
            return request.tool_choice.function.name
        if request.tool_choice == "required" and request.tools:
            return request.tools[0].function.name
        return None

    @staticmethod
    def _apply_output_cap(
        content: str | None, request: ChatCompletionRequest
    ) -> tuple[str | None, bool]:
        """Clip ``content`` to the request's output cap, reporting whether it hit."""
        cap = request.effective_max_tokens
        if content is None or cap is None:
            return content, False
        words = content.split()
        if len(words) <= cap:
            return content, False
        return " ".join(words[:cap]), True

    @staticmethod
    def _deltas(message: ResponseMessage) -> list[ChoiceDelta]:
        """Slice a finished message into the deltas a stream would emit."""
        deltas = [
            ChoiceDelta(content=token) for token in _STREAM_TOKEN.findall(message.content or "")
        ]
        if message.refusal:
            deltas.append(ChoiceDelta(refusal=message.refusal))
        for position, call in enumerate(message.tool_calls or []):
            # Name and arguments arrive separately, as they do upstream: a
            # consumer that assumes one whole call per chunk is broken, and this
            # is where that shows up.
            deltas.append(
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
            deltas.append(
                ChoiceDelta(
                    tool_calls=[
                        ToolCallDelta(
                            index=position,
                            function=FunctionCallDelta(arguments=call.function.arguments),
                        )
                    ]
                )
            )
        return deltas

    @staticmethod
    def _usage(request: ChatCompletionRequest, completion_tokens: int) -> TokenUsage:
        """Bill the prompt once and the completion once per choice."""
        prompt_tokens = _prompt_tokens(request.messages)
        generated = completion_tokens * request.n
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=generated,
            total_tokens=prompt_tokens + generated,
        )

    def _metadata(self, request: ChatCompletionRequest) -> GatewayMetadata:
        """Provenance saying a mock, not a vendor, produced this."""
        return GatewayMetadata(provider=self.name, upstream_model=request.model)
