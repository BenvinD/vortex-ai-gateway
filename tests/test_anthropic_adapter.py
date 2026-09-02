"""Tests for the Anthropic adapter — the translation, in both directions.

Every case here is a structural disagreement between OpenAI's chat format and
Anthropic's Messages API, plus the parameters that have no equivalent at all
and are therefore refused rather than dropped.
"""

from typing import Any

import httpx
import pytest

from tests.upstream import Upstream, chat_request, sse
from vortex_ai_gateway.providers import (
    AnthropicAdapter,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderUnavailable,
    TranslationError,
    UnsupportedParameterError,
)

MESSAGE: dict[str, Any] = {
    "id": "msg_123",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5-20250929",
    "content": [{"type": "text", "text": "pong"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 9,
        "output_tokens": 2,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 0,
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look a city up",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def adapter(upstream: Upstream, **kwargs: Any) -> AnthropicAdapter:
    return AnthropicAdapter(client=upstream.client(), **kwargs)


async def send(request_overrides: dict[str, Any]) -> dict[str, Any]:
    """Translate a request and return the body Anthropic would have received."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))
    await adapter(upstream).complete(chat_request(**request_overrides))
    return upstream.sent


# -- Request translation ----------------------------------------------------


async def test_system_prompts_are_hoisted_out_of_the_conversation() -> None:
    """Anthropic has no system *turn*, so they become one system string."""
    sent = await send(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "ping"},
                {"role": "developer", "content": "and be kind"},
            ]
        }
    )

    assert sent["system"] == "be brief\n\nand be kind"
    assert [turn["role"] for turn in sent["messages"]] == ["user"]


async def test_tool_results_become_blocks_in_the_next_user_turn() -> None:
    """Anthropic rejects two turns in a row from the same speaker."""
    sent = await send(
        {
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"city": "Oslo"}'},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"city": "Bergen"}'},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "rain"},
                {"role": "tool", "tool_call_id": "call_2", "content": "more rain"},
                {"role": "user", "content": "thanks"},
            ]
        }
    )

    assert [turn["role"] for turn in sent["messages"]] == ["user", "assistant", "user"]
    assert sent["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "lookup",
        "input": {"city": "Oslo"},
    }
    results = sent["messages"][2]["content"]
    assert [block["type"] for block in results] == ["tool_result", "tool_result", "text"]
    assert results[0]["tool_use_id"] == "call_1"


async def test_an_assistant_turn_with_nothing_to_say_is_dropped() -> None:
    """The contract allows empty text; Anthropic rejects an empty turn."""
    sent = await send(
        {
            "messages": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "still there?"},
            ]
        }
    )

    assert len(sent["messages"]) == 1
    assert [block["text"] for block in sent["messages"][0]["content"]] == ["ping", "still there?"]


async def test_an_output_cap_is_always_sent() -> None:
    """``max_tokens`` is optional in the contract and required by Anthropic."""
    assert (await send({}))["max_tokens"] == 4096
    assert (await send({"max_completion_tokens": 64}))["max_tokens"] == 64


async def test_temperature_is_clamped_to_anthropics_range() -> None:
    """OpenAI allows 2.0; sending that upstream is a 400."""
    assert (await send({"temperature": 1.7}))["temperature"] == 1.0
    assert (await send({"temperature": 0.3}))["temperature"] == 0.3


async def test_top_p_is_sent_only_when_the_caller_narrowed_it() -> None:
    """1.0 is the contract's "unset"; forwarding it would be an instruction."""
    assert "top_p" not in await send({})
    assert (await send({"top_p": 0.5}))["top_p"] == 0.5


@pytest.mark.parametrize(
    ("stop", "expected"),
    [("END", ["END"]), (["A", "B"], ["A", "B"])],
    ids=["one", "several"],
)
async def test_stop_becomes_a_sequence_list(stop: Any, expected: list[str]) -> None:
    """Anthropic takes only the list form of the same idea."""
    assert (await send({"stop": stop}))["stop_sequences"] == expected


async def test_tools_are_flattened_to_anthropics_shape() -> None:
    """``function.parameters`` becomes a top-level ``input_schema``."""
    sent = await send({"tools": TOOLS})

    assert sent["tools"] == [
        {
            "name": "lookup",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            "description": "Look a city up",
        }
    ]


@pytest.mark.parametrize(
    ("tool_choice", "expected"),
    [
        (None, None),
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ("required", {"type": "any"}),
        ({"type": "function", "function": {"name": "lookup"}}, {"type": "tool", "name": "lookup"}),
    ],
    ids=["unset", "auto", "none", "required", "named"],
)
async def test_tool_choice_is_mapped(tool_choice: Any, expected: dict[str, Any] | None) -> None:
    sent = await send({"tools": TOOLS, "tool_choice": tool_choice})

    assert sent.get("tool_choice") == expected


async def test_serial_tool_calling_hangs_off_the_tool_choice() -> None:
    """Anthropic expresses it as a flag on the choice, not a top-level field."""
    sent = await send({"tools": TOOLS, "parallel_tool_calls": False})

    assert sent["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


async def test_tool_choice_is_omitted_when_no_tools_are_declared() -> None:
    """A choice without tools is a guaranteed upstream rejection."""
    assert "tool_choice" not in await send({"parallel_tool_calls": False})


async def test_the_end_user_identifier_becomes_metadata() -> None:
    assert (await send({"user": "user-42"}))["metadata"] == {"user_id": "user-42"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"n": 2},
        {"seed": 7},
        {"logprobs": True},
        {"logprobs": True, "top_logprobs": 3},
        {"logit_bias": {"1234": 50}},
        {"frequency_penalty": 0.5},
        {"presence_penalty": 0.5},
        {"prediction": {"type": "content", "content": "draft"}},
        {"service_tier": "flex"},
        {"store": True},
        {"response_format": {"type": "json_object"}},
    ],
    ids=lambda overrides: "+".join(overrides),
)
async def test_a_parameter_anthropic_cannot_honour_is_refused(overrides: dict[str, Any]) -> None:
    """Silently dropping one would answer a question the caller did not ask."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))

    with pytest.raises(UnsupportedParameterError) as caught:
        await adapter(upstream).complete(chat_request(**overrides))

    assert caught.value.parameter in {*overrides, "top_logprobs"}
    assert not upstream.requests, "the request must not reach the vendor"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "data:image/png;base64,AAAA",
            {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        ),
        ("https://example.test/cat.png", {"type": "url", "url": "https://example.test/cat.png"}),
    ],
    ids=["inline", "hosted"],
)
async def test_images_are_translated_to_a_source_object(url: str, expected: dict[str, str]) -> None:
    sent = await send(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ]
        }
    )

    assert sent["messages"][0]["content"][1] == {"type": "image", "source": expected}


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (
            [{"type": "image_url", "image_url": {"url": "ftp://host/cat.png"}}],
            "Unsupported image URL scheme",
        ),
        (
            [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}],
            "no audio input",
        ),
    ],
    ids=["image-scheme", "audio"],
)
async def test_content_anthropic_cannot_carry_is_refused(
    content: list[dict[str, Any]], match: str
) -> None:
    upstream = Upstream(httpx.Response(200, json=MESSAGE))

    with pytest.raises(TranslationError, match=match):
        await adapter(upstream).complete(
            chat_request(messages=[{"role": "user", "content": content}])
        )


async def test_tool_arguments_that_are_not_json_are_refused() -> None:
    """Anthropic takes tool input as an object, so the string has to parse."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))
    request = chat_request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "not json"},
                    }
                ],
            },
        ]
    )

    with pytest.raises(TranslationError, match="call_1"):
        await adapter(upstream).complete(request)


async def test_the_api_key_and_version_are_sent_as_headers() -> None:
    """Anthropic authenticates with ``x-api-key``, not a bearer token."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))

    await adapter(upstream, api_key="sk-ant-test").complete(chat_request())

    assert upstream.headers["x-api-key"] == "sk-ant-test"
    assert upstream.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in upstream.headers


# -- Response translation ---------------------------------------------------


async def test_a_message_becomes_one_choice() -> None:
    """Anthropic returns a message; the contract returns a list of choices."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))

    response = await adapter(upstream).complete(chat_request())

    assert response.id == "msg_123"
    assert len(response.choices) == 1
    assert response.choices[0].message.content == "pong"
    assert response.choices[0].finish_reason == "stop"
    assert response.model == "test-model"
    assert response.vortex is not None
    assert response.vortex.upstream_model == "claude-sonnet-4-5-20250929"


async def test_the_prompt_total_includes_the_cached_share() -> None:
    """``input_tokens`` excludes cache hits, so the three counts are summed."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))

    usage = (await adapter(upstream).complete(chat_request())).usage

    assert usage is not None
    assert usage.prompt_tokens == 13
    assert usage.completion_tokens == 2
    assert usage.total_tokens == 15
    assert usage.prompt_tokens_details is not None
    assert usage.prompt_tokens_details.cached_tokens == 4


async def test_tool_use_blocks_become_tool_calls() -> None:
    """The block's ``input`` object is re-encoded as the contract's string."""
    message = MESSAGE | {
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"city": "Oslo"}},
        ],
        "stop_reason": "tool_use",
    }
    upstream = Upstream(httpx.Response(200, json=message))

    choice = (await adapter(upstream).complete(chat_request())).choices[0]

    assert choice.message.content == "checking"
    assert choice.message.tool_calls is not None
    assert choice.message.tool_calls[0].id == "toolu_1"
    assert choice.message.tool_calls[0].function.arguments == '{"city": "Oslo"}'
    assert choice.finish_reason == "tool_calls"


async def test_a_block_type_we_do_not_model_is_skipped() -> None:
    """An additive block must not cost the caller the whole answer."""
    message = MESSAGE | {
        "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "abc"},
            {"type": "text", "text": "pong"},
        ]
    }
    upstream = Upstream(httpx.Response(200, json=message))

    response = await adapter(upstream).complete(chat_request())

    assert response.choices[0].message.content == "pong"


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
        ("something_new", "stop"),
    ],
)
async def test_stop_reasons_are_mapped(stop_reason: str, expected: str) -> None:
    """An unrecognised reason reads as an ordinary stop, not a failure."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE | {"stop_reason": stop_reason}))

    response = await adapter(upstream).complete(chat_request())

    assert response.choices[0].finish_reason == expected


# -- Streaming --------------------------------------------------------------

STREAM_SCRIPT = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_stream",
            "model": "claude-sonnet-4-5-20250929",
            "usage": {"input_tokens": 9, "cache_read_input_tokens": 4},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "po"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ng"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '"Oslo"}'},
    },
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
    {"type": "message_stop"},
]


async def test_the_event_stream_becomes_openai_shaped_chunks() -> None:
    """A document being assembled, retold as a flat run of deltas."""
    upstream = Upstream(sse(*STREAM_SCRIPT))
    request = chat_request(stream=True, stream_options={"include_usage": True})

    chunks = [chunk async for chunk in adapter(upstream).stream(request)]

    assert chunks[0].choices[0].delta.role == "assistant"
    assert [c.choices[0].delta.content for c in chunks[1:3]] == ["po", "ng"]
    assert all(chunk.id == "msg_stream" for chunk in chunks)

    names = chunks[3].choices[0].delta.tool_calls
    assert names is not None
    assert names[0].id == "toolu_1"
    assert names[0].function is not None
    assert names[0].function.name == "lookup"

    arguments = [
        call.function.arguments
        for chunk in chunks[4:6]
        for call in chunk.choices[0].delta.tool_calls or []
        if call.function is not None
    ]
    assert "".join(arguments) == '{"city":"Oslo"}'

    assert chunks[-2].choices[0].finish_reason == "tool_calls"


async def test_tool_calls_are_numbered_by_call_not_by_block() -> None:
    """Anthropic indexes every block; OpenAI indexes only the tool calls."""
    script = [
        STREAM_SCRIPT[0],
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {"type": "tool_use", "id": "toolu_9", "name": "lookup", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {"type": "message_stop"},
    ]
    upstream = Upstream(sse(*script))

    chunks = [chunk async for chunk in adapter(upstream).stream(chat_request(stream=True))]
    indexes = [call.index for chunk in chunks for call in chunk.choices[0].delta.tool_calls or []]

    assert indexes == [0, 0]


async def test_usage_arrives_in_a_final_chunk_only_when_asked_for() -> None:
    """The usage chunk is opt-in, as it is on OpenAI."""
    upstream = Upstream(sse(*STREAM_SCRIPT))
    request = chat_request(stream=True, stream_options={"include_usage": True})

    chunks = [chunk async for chunk in adapter(upstream).stream(request)]

    assert chunks[-1].choices == []
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 13
    assert chunks[-1].usage.completion_tokens == 12

    silent = Upstream(sse(*STREAM_SCRIPT))
    quiet = [chunk async for chunk in adapter(silent).stream(chat_request(stream=True))]
    assert all(chunk.usage is None for chunk in quiet)


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("overloaded_error", ProviderUnavailable),
        ("rate_limit_error", ProviderRateLimited),
        ("api_error", ProviderUnavailable),
    ],
)
async def test_an_error_event_mid_stream_is_classified(
    error_type: str, expected: type[Exception]
) -> None:
    """A stream that dies half way is a failure, not a short answer.

    There is no status code to classify by — the ``200`` went out with the
    first chunk — so the vendor's own error type has to carry it.
    """
    script = [
        STREAM_SCRIPT[0],
        {"type": "error", "error": {"type": error_type, "message": "Overloaded"}},
    ]
    upstream = Upstream(sse(*script))

    with pytest.raises(expected, match="Overloaded") as caught:
        async for _ in adapter(upstream).stream(chat_request(stream=True)):
            pass

    assert caught.value.code == error_type


# -- Answers that make no sense ---------------------------------------------


async def test_content_that_is_not_a_list_of_blocks_is_a_protocol_error() -> None:
    upstream = Upstream(httpx.Response(200, json=MESSAGE | {"content": "pong"}))

    with pytest.raises(ProviderProtocolError, match="array of blocks"):
        await adapter(upstream).complete(chat_request())


async def test_a_tool_use_block_without_a_name_is_a_protocol_error() -> None:
    """A call the gateway cannot name is one the caller could never answer."""
    message = MESSAGE | {"content": [{"type": "tool_use", "id": "toolu_1", "input": {}}]}
    upstream = Upstream(httpx.Response(200, json=message))

    with pytest.raises(ProviderProtocolError, match="missing its id or name"):
        await adapter(upstream).complete(chat_request())


async def test_counts_that_cannot_be_true_are_a_protocol_error() -> None:
    """The contract's own validation is the last check on a decoded answer."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE | {"usage": {"input_tokens": -5}}))

    with pytest.raises(ProviderProtocolError, match="greater than or equal to 0"):
        await adapter(upstream).complete(chat_request())


async def test_an_error_event_without_detail_still_raises() -> None:
    upstream = Upstream(sse(STREAM_SCRIPT[0], {"type": "error", "error": "overloaded"}))

    with pytest.raises(ProviderUnavailable, match="overloaded"):
        async for _ in adapter(upstream).stream(chat_request(stream=True)):
            pass


# -- Remaining translation corners ------------------------------------------


async def test_multipart_text_is_joined_before_it_is_sent() -> None:
    """Anthropic's system prompt is one string, however the caller split it."""
    sent = await send(
        {
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "be brief"},
                        {"type": "text", "text": "be kind"},
                    ],
                },
                {"role": "user", "content": "ping"},
            ]
        }
    )

    assert sent["system"] == "be brief\nbe kind"


async def test_an_assistant_turn_keeps_its_words_and_its_refusal() -> None:
    """Both are text to Anthropic, which models no separate refusal field."""
    sent = await send(
        {
            "messages": [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "I looked", "refusal": "but I will not say"},
                {"role": "user", "content": "why?"},
            ]
        }
    )

    assert [block["text"] for block in sent["messages"][1]["content"]] == [
        "I looked",
        "but I will not say",
    ]


async def test_tool_arguments_that_are_not_an_object_are_refused() -> None:
    """Valid JSON is not enough; Anthropic's tool input must be an object."""
    upstream = Upstream(httpx.Response(200, json=MESSAGE))
    request = chat_request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "[1, 2]"},
                    }
                ],
            },
        ]
    )

    with pytest.raises(TranslationError, match="list arguments"):
        await adapter(upstream).complete(request)


async def test_an_image_that_is_not_base64_is_refused() -> None:
    upstream = Upstream(httpx.Response(200, json=MESSAGE))
    content = [{"type": "image_url", "image_url": {"url": "data:image/png,rawbytes"}}]

    with pytest.raises(TranslationError, match="only base64 data URLs"):
        await adapter(upstream).complete(
            chat_request(messages=[{"role": "user", "content": content}])
        )


async def test_text_that_arrives_with_the_block_is_not_lost() -> None:
    """Anthropic may prime a text block with content at the moment it opens."""
    script = [
        STREAM_SCRIPT[0],
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "already "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "here"},
        },
        {"type": "message_stop"},
    ]
    upstream = Upstream(sse(*script))

    chunks = [chunk async for chunk in adapter(upstream).stream(chat_request(stream=True))]

    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "already here"


async def test_a_message_with_no_content_at_all_is_still_a_completion() -> None:
    """An empty answer is an answer; it is not a reason to fail the request."""
    message = {key: value for key, value in MESSAGE.items() if key != "content"}
    upstream = Upstream(httpx.Response(200, json=message))

    response = await adapter(upstream).complete(chat_request())

    assert response.choices[0].message.content is None
    assert response.choices[0].message.tool_calls is None
