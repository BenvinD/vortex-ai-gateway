"""Tests for the scripted, zero-cost provider."""

import json
from typing import Any

import pytest

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    FunctionCall,
    ToolCall,
)
from vortex_ai_gateway.providers import (
    MOCK_CREATED,
    CannedReply,
    ChatProvider,
    MockProvider,
)


def build_request(**overrides: Any) -> ChatCompletionRequest:
    """A minimal valid request, plus any overrides."""
    body: dict[str, Any] = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    } | overrides
    return ChatCompletionRequest.model_validate(body)


async def collect(
    provider: MockProvider, request: ChatCompletionRequest
) -> list[ChatCompletionChunk]:
    """Drain a stream into a list."""
    return [chunk async for chunk in provider.stream(request)]


def test_mock_provider_satisfies_the_chat_provider_protocol() -> None:
    """The mock is substitutable for a real adapter."""
    assert isinstance(MockProvider(), ChatProvider)


async def test_derived_reply_echoes_the_last_user_turn() -> None:
    """With no script, the reply is derived from the request."""
    response = await MockProvider().complete(build_request())

    assert response.choices[0].message.content == "mock reply to: ping"
    assert response.choices[0].finish_reason == "stop"
    assert response.model == "gpt-4o-mini"


async def test_reply_without_a_user_turn_still_answers() -> None:
    """A system-only prompt is valid, so the mock must not assume a user turn."""
    response = await MockProvider().complete(
        build_request(messages=[{"role": "system", "content": "be brief"}])
    )

    assert response.choices[0].message.content == "mock reply"


async def test_response_is_deterministic() -> None:
    """IDs count up from a fixed prefix and `created` never moves."""
    provider = MockProvider()
    first = await provider.complete(build_request())
    second = await provider.complete(build_request())

    assert first.id == "chatcmpl-mock-001"
    assert second.id == "chatcmpl-mock-002"
    assert first.created == second.created == MOCK_CREATED


async def test_response_is_attributed_to_the_mock() -> None:
    """Provenance says a mock served the call, not a vendor."""
    response = await MockProvider(name="mock-openai").complete(build_request())

    assert response.vortex is not None
    assert response.vortex.provider == "mock-openai"
    assert response.vortex.upstream_model == "gpt-4o-mini"


async def test_requests_are_recorded_for_assertion() -> None:
    """What the gateway decided to send is usually the real assertion."""
    provider = MockProvider()
    await provider.complete(build_request(temperature=0.2))

    assert provider.call_count == 1
    assert provider.received_requests[0].temperature == 0.2


async def test_reset_rewinds_the_script_and_the_log() -> None:
    """A provider can be reused across cases without rebuilding it."""
    provider = MockProvider(replies=[CannedReply("one"), CannedReply("two")])
    await provider.complete(build_request())
    provider.reset()
    response = await provider.complete(build_request())

    assert response.choices[0].message.content == "one"
    assert provider.received_requests == [provider.received_requests[0]]


async def test_scripted_replies_play_in_order_then_repeat() -> None:
    """The last entry sticks, so a loop never falls off the end of the script."""
    provider = MockProvider(replies=[CannedReply("first"), CannedReply("second")])
    contents = [
        (await provider.complete(build_request())).choices[0].message.content for _ in range(3)
    ]

    assert contents == ["first", "second", "second"]


async def test_scripted_exception_is_raised_not_returned() -> None:
    """Failure paths are exercised by putting an error in the script."""
    provider = MockProvider(replies=[RuntimeError("upstream on fire"), CannedReply("recovered")])

    with pytest.raises(RuntimeError, match="upstream on fire"):
        await provider.complete(build_request())

    response = await provider.complete(build_request())
    assert response.choices[0].message.content == "recovered"
    assert provider.call_count == 2


async def test_n_choices_are_generated_and_billed() -> None:
    """The prompt is billed once; the completion once per choice."""
    response = await MockProvider(replies=[CannedReply("a b")]).complete(
        build_request(n=3, messages=[{"role": "user", "content": "one two three"}])
    )

    assert [choice.index for choice in response.choices] == [0, 1, 2]
    assert response.usage is not None
    assert response.usage.prompt_tokens == 3
    assert response.usage.completion_tokens == 6
    assert response.usage.total_tokens == 9


async def test_output_cap_truncates_and_reports_length() -> None:
    """The cap applies to canned content too, so scripts stay reusable."""
    provider = MockProvider(replies=[CannedReply("one two three four")])
    response = await provider.complete(build_request(max_completion_tokens=2))

    assert response.choices[0].message.content == "one two"
    assert response.choices[0].finish_reason == "length"
    assert response.usage is not None
    assert response.usage.completion_tokens == 2


async def test_reply_within_the_cap_is_untouched() -> None:
    """A cap that is never reached changes nothing."""
    provider = MockProvider(replies=[CannedReply("one two")])
    response = await provider.complete(build_request(max_tokens=50))

    assert response.choices[0].message.content == "one two"
    assert response.choices[0].finish_reason == "stop"


async def test_forced_tool_choice_produces_a_matching_call() -> None:
    """A request that demands a tool gets one back without a script."""
    request = build_request(
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )
    response = await MockProvider().complete(request)

    message = response.choices[0].message
    assert message.tool_calls is not None
    assert message.tool_calls[0].function.name == "get_weather"
    assert message.content is None
    assert response.choices[0].finish_reason == "tool_calls"


async def test_required_tool_choice_falls_back_to_the_first_tool() -> None:
    """`required` with no name picks a declared tool rather than answering in text."""
    request = build_request(
        tools=[
            {"type": "function", "function": {"name": "search"}},
            {"type": "function", "function": {"name": "lookup"}},
        ],
        tool_choice="required",
    )
    response = await MockProvider().complete(request)

    assert response.choices[0].message.tool_calls is not None
    assert response.choices[0].message.tool_calls[0].function.name == "search"


async def test_json_response_format_yields_parseable_content() -> None:
    """Asking for JSON gets JSON, so response-format handling can be tested."""
    response = await MockProvider().complete(build_request(response_format={"type": "json_object"}))

    content = response.choices[0].message.content
    assert content is not None
    assert json.loads(content) == {"echo": "ping"}


async def test_canned_finish_reason_survives() -> None:
    """A script can assert an unusual terminal state, e.g. a content filter."""
    provider = MockProvider(replies=[CannedReply("blocked", finish_reason="content_filter")])
    response = await provider.complete(build_request())

    assert response.choices[0].finish_reason == "content_filter"


async def test_stream_reassembles_into_the_completed_content() -> None:
    """Concatenated deltas equal the whole reply, exactly."""
    provider = MockProvider(replies=[CannedReply("mock streams  in pieces")])
    chunks = await collect(provider, build_request(stream=True))

    assert chunks[0].choices[0].delta.role == "assistant"
    streamed = "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices)
    assert streamed == "mock streams  in pieces"
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert all(chunk.object == "chat.completion.chunk" for chunk in chunks)


async def test_stream_omits_usage_unless_asked() -> None:
    """Usage on a stream is opt-in, as it is upstream."""
    chunks = await collect(MockProvider(), build_request(stream=True))

    assert all(chunk.usage is None for chunk in chunks)


async def test_stream_appends_a_usage_chunk_when_requested() -> None:
    """The usage chunk comes last and carries no choices."""
    request = build_request(stream=True, stream_options={"include_usage": True})
    chunks = await collect(MockProvider(), request)

    final = chunks[-1]
    assert final.choices == []
    assert final.usage is not None
    assert final.usage.total_tokens == final.usage.prompt_tokens + final.usage.completion_tokens


async def test_stream_splits_a_tool_call_across_chunks() -> None:
    """Name and arguments arrive separately, as they do from a real provider."""
    reply = CannedReply(
        tool_calls=(
            ToolCall(id="call_1", function=FunctionCall(name="search", arguments='{"q":"x"}')),
        )
    )
    chunks = await collect(MockProvider(replies=[reply]), build_request(stream=True))

    deltas = [
        chunk.choices[0].delta.tool_calls[0]
        for chunk in chunks
        if chunk.choices and chunk.choices[0].delta.tool_calls
    ]
    assert [delta.function.name for delta in deltas if delta.function] == ["search", None]
    assert [delta.function.arguments for delta in deltas if delta.function] == [None, '{"q":"x"}']
    assert deltas[0].id == "call_1"
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def test_stream_covers_every_choice() -> None:
    """`n` is honoured on the streaming path too."""
    chunks = await collect(MockProvider(), build_request(stream=True, n=2))

    seen = {choice.index for chunk in chunks for choice in chunk.choices}
    assert seen == {0, 1}


async def test_refusal_is_streamed_and_returned() -> None:
    """A refusal is a first-class reply, not empty content."""
    provider = MockProvider(replies=[CannedReply(refusal="I can't help with that")])
    response = await provider.complete(build_request())
    chunks = await collect(MockProvider(replies=[CannedReply(refusal="nope")]), build_request())

    assert response.choices[0].message.refusal == "I can't help with that"
    assert response.choices[0].message.content is None
    assert any(chunk.choices[0].delta.refusal == "nope" for chunk in chunks if chunk.choices)


async def test_configured_latency_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout and budget behaviour can be tested without real waiting."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("vortex_ai_gateway.providers.mock.asyncio.sleep", record)
    await MockProvider(latency_seconds=1.5).complete(build_request())

    assert slept == [1.5]


async def test_no_latency_means_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default path never touches the event loop's clock."""

    async def fail(seconds: float) -> None:
        raise AssertionError("should not sleep")

    monkeypatch.setattr("vortex_ai_gateway.providers.mock.asyncio.sleep", fail)
    await MockProvider().complete(build_request())


async def test_prompt_tokens_count_tool_calls_in_history() -> None:
    """A replayed tool call is billed, so budget tests see the real prompt."""
    request = build_request(
        messages=[
            {"role": "user", "content": "one two"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "three", "tool_call_id": "call_1"},
        ]
    )
    response = await MockProvider(replies=[CannedReply("x")]).complete(request)

    assert response.usage is not None
    assert response.usage.prompt_tokens == 5


async def test_multimodal_prompt_counts_only_its_text() -> None:
    """Image parts carry no words; the text alongside them still counts."""
    request = build_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                ],
            }
        ]
    )
    response = await MockProvider(replies=[CannedReply("ok")]).complete(request)

    assert response.usage is not None
    assert response.usage.prompt_tokens == 2
    assert response.choices[0].message.content == "ok"
