"""Tests for the OpenAI adapter.

Because the contract is OpenAI's own shape, the assertions here are mostly
about what the adapter *refuses* to change: a request goes out as the caller
wrote it, and a response comes back with nothing dropped.
"""

from typing import Any

import httpx
import pytest

from tests.upstream import Upstream, chat_request, sse
from vortex_ai_gateway.providers import OpenAIAdapter, ProviderProtocolError

COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "gpt-4o-mini-2024-07-18",
    "system_fingerprint": "fp_44709d6fcb",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 9,
        "completion_tokens": 2,
        "total_tokens": 11,
        "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 0},
    },
}


def chunk(**overrides: Any) -> dict[str, Any]:
    """One streamed chunk in OpenAI's wire format."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    } | overrides


def adapter(upstream: Upstream, **kwargs: Any) -> OpenAIAdapter:
    return OpenAIAdapter(client=upstream.client(), **kwargs)


async def test_the_request_is_forwarded_field_for_field() -> None:
    """No translation means no opportunity to lose a parameter."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    request = chat_request(
        temperature=0.2,
        n=3,
        seed=42,
        stop=["\n\n"],
        logprobs=True,
        top_logprobs=5,
        parallel_tool_calls=False,
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    )

    await adapter(upstream).complete(request)

    sent = upstream.sent
    assert sent["temperature"] == 0.2
    assert sent["n"] == 3
    assert sent["seed"] == 42
    assert sent["stop"] == ["\n\n"]
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] == 5
    assert sent["parallel_tool_calls"] is False
    assert sent["tools"][0]["function"]["name"] == "lookup"
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}


async def test_unset_fields_are_omitted_rather_than_sent_as_null() -> None:
    """The contract spells "unset" as null; OpenAI reads null as a value."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    await adapter(upstream).complete(chat_request())

    assert "stop" not in upstream.sent
    assert "tools" not in upstream.sent
    assert "max_completion_tokens" not in upstream.sent


async def test_a_json_schema_keeps_the_name_openai_expects() -> None:
    """The contract renames ``schema``; the wire format must not see that."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    request = chat_request(
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "reply", "schema": {"type": "object"}},
        }
    )

    await adapter(upstream).complete(request)

    assert upstream.sent["response_format"]["json_schema"]["schema"] == {"type": "object"}


async def test_only_the_current_output_cap_spelling_is_sent() -> None:
    """Sending both spellings is rejected upstream, so one of them goes."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    await adapter(upstream).complete(chat_request(max_tokens=16, max_completion_tokens=16))

    assert upstream.sent["max_completion_tokens"] == 16
    assert "max_tokens" not in upstream.sent


async def test_a_buffered_request_is_marked_not_streaming() -> None:
    """``complete`` never depends on what the caller put in ``stream``."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    await adapter(upstream).complete(
        chat_request(stream=True, stream_options={"include_usage": True})
    )

    assert upstream.sent["stream"] is False
    assert "stream_options" not in upstream.sent


async def test_a_streaming_request_carries_its_stream_options() -> None:
    """Usage reporting is opt-in, and the opt-in has to reach the vendor."""
    upstream = Upstream(sse("[DONE]"))
    request = chat_request(stream=True, stream_options={"include_usage": True})

    async for _ in adapter(upstream).stream(request):
        pass

    assert upstream.sent["stream"] is True
    assert upstream.sent["stream_options"] == {"include_usage": True}


async def test_the_response_survives_the_trip_intact() -> None:
    """Everything OpenAI reported is still there after translation."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    response = await adapter(upstream).complete(chat_request())

    assert response.id == "chatcmpl-1"
    assert response.created == 1_700_000_000
    assert response.system_fingerprint == "fp_44709d6fcb"
    assert response.choices[0].message.content == "pong"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.total_tokens == 11
    assert response.usage.prompt_tokens_details is not None
    assert response.usage.prompt_tokens_details.cached_tokens == 4


async def test_tool_calls_come_back_whole() -> None:
    """The argument string is passed through, not re-encoded."""
    completion = COMPLETION | {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"city": "Oslo"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    upstream = Upstream(httpx.Response(200, json=completion))

    response = await adapter(upstream).complete(chat_request())

    calls = response.choices[0].message.tool_calls
    assert calls is not None
    assert calls[0].id == "call_abc"
    assert calls[0].function.arguments == '{"city": "Oslo"}'
    assert response.choices[0].finish_reason == "tool_calls"


async def test_an_unmodelled_response_field_fails_loudly() -> None:
    """The strict-contract trade of ADR-010, at the point where it bites.

    A field OpenAI adds that ``contracts/`` does not know about stops the
    request rather than vanishing from the response. That is deliberate: a
    silently dropped field is a bug the caller cannot see, let alone report.
    """
    upstream = Upstream(httpx.Response(200, json=COMPLETION | {"a_new_field": "surprise"}))

    with pytest.raises(ProviderProtocolError, match="a_new_field"):
        await adapter(upstream).complete(chat_request())


async def test_the_stream_is_relayed_chunk_for_chunk() -> None:
    """Streaming is a relay: the chunks are already in the right shape."""
    upstream = Upstream(
        sse(
            chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}}]),
            chunk(choices=[{"index": 0, "delta": {"content": "po"}}]),
            chunk(choices=[{"index": 0, "delta": {"content": "ng"}}]),
            chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            "[DONE]",
        )
    )

    chunks = [
        c
        async for c in adapter(upstream, models={"test-model": "gpt-4o-mini"}).stream(
            chat_request(stream=True)
        )
    ]

    assert [c.choices[0].delta.content for c in chunks] == ["", "po", "ng", None]
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert all(c.model == "test-model" for c in chunks)
    assert chunks[0].vortex is not None
    assert chunks[0].vortex.upstream_model == "gpt-4o-mini-2024-07-18"


async def test_the_stream_stops_at_the_terminator() -> None:
    """Anything after ``[DONE]`` is not part of the completion."""
    upstream = Upstream(sse(chunk(), "[DONE]", chunk()))

    chunks = [c async for c in adapter(upstream).stream(chat_request(stream=True))]

    assert len(chunks) == 1


async def test_the_api_key_is_sent_as_a_bearer_token() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    await adapter(upstream, api_key="sk-test").complete(chat_request())

    assert upstream.headers["authorization"] == "Bearer sk-test"


async def test_an_unmodelled_field_in_a_chunk_fails_loudly_too() -> None:
    """Streaming gets the same strictness as the buffered path, not less."""
    upstream = Upstream(sse(chunk(a_new_field="surprise"), "[DONE]"))

    with pytest.raises(ProviderProtocolError, match="a_new_field"):
        async for _ in adapter(upstream).stream(chat_request(stream=True)):
            pass
