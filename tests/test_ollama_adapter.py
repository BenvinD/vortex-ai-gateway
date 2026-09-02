"""Tests for the Ollama adapter.

Ollama is close to the contract, so the cases worth writing down are the three
places it is not: sampling nested under ``options``, tool calls with no IDs,
and a JSON Lines stream instead of server-sent events.
"""

import json
from typing import Any

import httpx
import pytest

from tests.upstream import Upstream, chat_request, ndjson
from vortex_ai_gateway.providers import (
    OllamaAdapter,
    ProviderProtocolError,
    TranslationError,
    UnsupportedParameterError,
)

COMPLETION: dict[str, Any] = {
    "model": "llama3.2",
    "created_at": "2024-05-01T12:00:00.000000Z",
    "message": {"role": "assistant", "content": "pong"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 9,
    "eval_count": 2,
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


def adapter(upstream: Upstream, **kwargs: Any) -> OllamaAdapter:
    return OllamaAdapter(client=upstream.client(), **kwargs)


async def send(request_overrides: dict[str, Any]) -> dict[str, Any]:
    """Translate a request and return the body Ollama would have received."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    await adapter(upstream).complete(chat_request(**request_overrides))
    return upstream.sent


# -- Request translation ----------------------------------------------------


async def test_default_sampling_is_left_to_the_modelfile() -> None:
    """An option in the body overrides the model's own configured value."""
    sent = await send({})

    assert "options" not in sent
    assert sent["stream"] is False


async def test_the_settings_the_caller_chose_are_nested_under_options() -> None:
    sent = await send(
        {
            "temperature": 0.2,
            "top_p": 0.8,
            "seed": 7,
            "frequency_penalty": 0.4,
            "presence_penalty": 0.1,
            "stop": ["END"],
            "max_completion_tokens": 64,
        }
    )

    assert sent["options"] == {
        "temperature": 0.2,
        "top_p": 0.8,
        "seed": 7,
        "frequency_penalty": 0.4,
        "presence_penalty": 0.1,
        "stop": ["END"],
        "num_predict": 64,
    }


@pytest.mark.parametrize(
    ("response_format", "expected"),
    [
        ({"type": "text"}, None),
        ({"type": "json_object"}, "json"),
        (
            {"type": "json_schema", "json_schema": {"name": "reply", "schema": {"type": "object"}}},
            {"type": "object"},
        ),
    ],
    ids=["text", "json", "schema"],
)
async def test_structured_output_maps_onto_format(
    response_format: dict[str, Any], expected: Any
) -> None:
    """Ollama takes either the word ``json`` or the schema itself."""
    assert (await send({"response_format": response_format})).get("format") == expected


async def test_tool_declarations_pass_through_unchanged() -> None:
    """Ollama adopted OpenAI's tool shape, so there is nothing to translate."""
    assert (await send({"tools": TOOLS}))["tools"] == TOOLS


async def test_a_developer_message_is_sent_as_a_system_one() -> None:
    """``developer`` is OpenAI's newer spelling; Ollama knows only the old."""
    sent = await send({"messages": [{"role": "developer", "content": "be brief"}]})

    assert sent["messages"] == [{"role": "system", "content": "be brief"}]


async def test_images_are_split_out_of_the_content() -> None:
    """Ollama takes text and a flat list of base64 images, not content parts."""
    sent = await send(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ]
        }
    )

    assert sent["messages"] == [{"role": "user", "content": "what is this?", "images": ["AAAA"]}]


async def test_a_tool_result_is_labelled_with_the_name_it_answers() -> None:
    """Ollama matches a result to its call by name, having issued no ID."""
    sent = await send(
        {
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"city": "Oslo"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "rain"},
            ]
        }
    )

    assert sent["messages"][1]["tool_calls"] == [
        {"function": {"name": "lookup", "arguments": {"city": "Oslo"}}}
    ]
    assert sent["messages"][2] == {"role": "tool", "content": "rain", "tool_name": "lookup"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"n": 2},
        {"logprobs": True},
        {"logit_bias": {"1234": 50}},
        {"prediction": {"type": "content", "content": "draft"}},
        {"service_tier": "flex"},
        {"store": True},
        {"tool_choice": "required", "tools": TOOLS},
        {"tool_choice": "none", "tools": TOOLS},
    ],
    ids=lambda overrides: "+".join(overrides),
)
async def test_a_parameter_ollama_cannot_honour_is_refused(overrides: dict[str, Any]) -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    with pytest.raises(UnsupportedParameterError):
        await adapter(upstream).complete(chat_request(**overrides))

    assert not upstream.requests, "the request must not reach the runtime"


async def test_letting_the_model_decide_is_supported() -> None:
    """``auto`` is Ollama's only behaviour, so it is not a rejection."""
    assert "tool_choice" not in await send({"tool_choice": "auto", "tools": TOOLS})


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (
            [{"type": "image_url", "image_url": {"url": "https://example.test/cat.png"}}],
            "does not fetch images by URL",
        ),
        (
            [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}],
            "no audio input",
        ),
    ],
    ids=["hosted-image", "audio"],
)
async def test_content_ollama_cannot_carry_is_refused(
    content: list[dict[str, Any]], match: str
) -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    with pytest.raises(TranslationError, match=match):
        await adapter(upstream).complete(
            chat_request(messages=[{"role": "user", "content": content}])
        )


async def test_no_credentials_are_sent_unless_configured() -> None:
    """A loopback runtime needs no key; a hosted one takes a bearer token."""
    plain = Upstream(httpx.Response(200, json=COMPLETION))
    await adapter(plain).complete(chat_request())
    assert "authorization" not in plain.headers

    hosted = Upstream(httpx.Response(200, json=COMPLETION))
    await adapter(hosted, api_key="token").complete(chat_request())
    assert hosted.headers["authorization"] == "Bearer token"


# -- Response translation ---------------------------------------------------


async def test_a_completion_is_translated_whole() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION))

    response = await adapter(upstream).complete(chat_request())

    assert response.choices[0].message.content == "pong"
    assert response.choices[0].finish_reason == "stop"
    assert response.created == 1_714_564_800  # the runtime's clock, not ours
    assert response.usage is not None
    assert response.usage.prompt_tokens == 9
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 11
    assert response.vortex is not None
    assert response.vortex.upstream_model == "llama3.2"


async def test_tool_calls_are_given_the_id_ollama_omits() -> None:
    """The contract requires an ID and an argument string; Ollama sends neither."""
    completion = COMPLETION | {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "lookup", "arguments": {"city": "Oslo"}}}],
        }
    }
    upstream = Upstream(httpx.Response(200, json=completion))

    choice = (await adapter(upstream).complete(chat_request())).choices[0]

    assert choice.message.content is None
    assert choice.message.tool_calls is not None
    assert choice.message.tool_calls[0].id == "call_0_lookup"
    assert choice.message.tool_calls[0].function.arguments == '{"city": "Oslo"}'
    assert choice.finish_reason == "tool_calls"


async def test_a_truncated_completion_reports_length() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION | {"done_reason": "length"}))

    response = await adapter(upstream).complete(chat_request())

    assert response.choices[0].finish_reason == "length"


async def test_a_missing_timestamp_does_not_read_as_1970() -> None:
    """Falling back to zero would render as 1970 in every client."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION | {"created_at": ""}))

    response = await adapter(upstream).complete(chat_request())

    assert response.created > 1_700_000_000


# -- Streaming --------------------------------------------------------------


async def test_json_lines_become_openai_shaped_chunks() -> None:
    upstream = Upstream(
        ndjson(
            {"model": "llama3.2", "message": {"role": "assistant", "content": "po"}, "done": False},
            {"model": "llama3.2", "message": {"role": "assistant", "content": "ng"}, "done": False},
            COMPLETION | {"message": {"role": "assistant", "content": ""}},
        )
    )
    request = chat_request(stream=True, stream_options={"include_usage": True})

    chunks = [chunk async for chunk in adapter(upstream).stream(request)]

    assert upstream.sent["stream"] is True
    assert chunks[0].choices[0].delta.role == "assistant"
    assert [c.choices[0].delta.content for c in chunks[1:3]] == ["po", "ng"]
    assert chunks[3].choices[0].finish_reason == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 11
    assert len({chunk.id for chunk in chunks}) == 1


async def test_a_streamed_tool_call_is_split_into_a_name_and_its_arguments() -> None:
    """Ollama sends a call whole; an OpenAI client expects to reassemble one."""
    upstream = Upstream(
        ndjson(
            {
                "model": "llama3.2",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "lookup", "arguments": {"city": "Oslo"}}}],
                },
                "done": False,
            },
            COMPLETION | {"message": {"role": "assistant", "content": ""}},
        )
    )

    chunks = [chunk async for chunk in adapter(upstream).stream(chat_request(stream=True))]
    calls = [call for chunk in chunks for call in chunk.choices[0].delta.tool_calls or []]

    assert [call.function.name for call in calls if call.function] == ["lookup", None]
    assert [call.function.arguments for call in calls if call.function] == [
        None,
        '{"city": "Oslo"}',
    ]
    assert calls[0].id == "call_0_lookup"
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


# -- Answers that make no sense ---------------------------------------------


async def test_tool_calls_that_are_not_a_list_are_a_protocol_error() -> None:
    completion = COMPLETION | {
        "message": {"role": "assistant", "content": "", "tool_calls": {"function": {}}}
    }
    upstream = Upstream(httpx.Response(200, json=completion))

    with pytest.raises(ProviderProtocolError, match="expected 'tool_calls' to be an array"):
        await adapter(upstream).complete(chat_request())


async def test_a_tool_call_without_a_name_is_a_protocol_error() -> None:
    completion = COMPLETION | {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"arguments": {}}}],
        }
    }
    upstream = Upstream(httpx.Response(200, json=completion))

    with pytest.raises(ProviderProtocolError, match="missing its function name"):
        await adapter(upstream).complete(chat_request())


async def test_counts_that_cannot_be_true_are_a_protocol_error() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION | {"eval_count": -3}))

    with pytest.raises(ProviderProtocolError, match="greater than or equal to 0"):
        await adapter(upstream).complete(chat_request())


async def test_an_unparseable_timestamp_falls_back_to_now() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION | {"created_at": "last tuesday"}))

    response = await adapter(upstream).complete(chat_request())

    assert response.created > 1_700_000_000


async def test_blank_lines_in_the_stream_are_ignored() -> None:
    """A JSON Lines body may be padded; a blank line is not a chunk."""
    body = (
        json.dumps({"model": "llama3.2", "message": {"content": "po"}, "done": False})
        + "\n\n"
        + json.dumps(COMPLETION | {"message": {"role": "assistant", "content": ""}})
        + "\n"
    )
    upstream = Upstream(httpx.Response(200, text=body))

    chunks = [chunk async for chunk in adapter(upstream).stream(chat_request(stream=True))]

    assert [c.choices[0].delta.content for c in chunks] == [None, "po", None]


# -- Remaining translation corners ------------------------------------------


async def test_multipart_text_is_joined_before_it_is_sent() -> None:
    """Ollama's message content is one string, however the caller split it."""
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

    assert sent["messages"][0]["content"] == "be brief\nbe kind"


async def test_an_image_that_is_not_base64_is_refused() -> None:
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    content = [{"type": "image_url", "image_url": {"url": "data:image/png,rawbytes"}}]

    with pytest.raises(TranslationError, match="images as base64"):
        await adapter(upstream).complete(
            chat_request(messages=[{"role": "user", "content": content}])
        )


@pytest.mark.parametrize(
    ("arguments", "match"),
    [("not json", "not JSON"), ("[1, 2]", "list arguments")],
    ids=["unparseable", "not-an-object"],
)
async def test_tool_arguments_that_are_not_an_object_are_refused(
    arguments: str, match: str
) -> None:
    """Ollama takes tool input as an object, so the string has to parse."""
    upstream = Upstream(httpx.Response(200, json=COMPLETION))
    request = chat_request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": arguments},
                    }
                ],
            },
        ]
    )

    with pytest.raises(TranslationError, match=match):
        await adapter(upstream).complete(request)
