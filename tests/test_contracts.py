"""Tests for the unified, OpenAI-compatible request/response contract."""

from typing import Any

import pytest
from pydantic import ValidationError

from vortex_ai_gateway.contracts import (
    AssistantMessage,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
    ImageContentPart,
    ModelCard,
    ModelList,
    ResponseMessage,
    TokenUsage,
    ToolMessage,
    UserMessage,
    error_response_from_validation_error,
    format_parameter_path,
)


def minimal_request(**overrides: Any) -> dict[str, Any]:
    """A request body with only the required keys, plus any overrides."""
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hello"}],
    } | overrides


def test_minimal_request_applies_openai_defaults() -> None:
    """Only model and messages are required; the rest match OpenAI's defaults."""
    request = ChatCompletionRequest.model_validate(minimal_request())

    assert request.temperature == 1.0
    assert request.top_p == 1.0
    assert request.n == 1
    assert request.stream is False
    assert request.parallel_tool_calls is True
    assert request.effective_max_tokens is None


def test_unknown_parameter_is_rejected_by_name() -> None:
    """A typo'd parameter fails loudly instead of being silently dropped."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(minimal_request(temperture=0.5))

    detail = error_response_from_validation_error(caught.value).error
    assert detail.param == "temperture"
    assert detail.code == "extra_forbidden"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 2.5),
        ("top_p", 0.0),
        ("n", 0),
        ("frequency_penalty", -3.0),
        ("presence_penalty", 2.1),
        ("max_completion_tokens", 0),
        ("top_logprobs", 21),
    ],
)
def test_out_of_range_sampling_parameters_are_rejected(field: str, value: float) -> None:
    """Every bounded knob is checked here, before any provider round trip."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(minimal_request(**{field: value}))


def test_empty_message_list_is_rejected() -> None:
    """There is nothing to complete without at least one message."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate({"model": "gpt-4o-mini", "messages": []})


def test_message_role_selects_the_message_shape() -> None:
    """The discriminator picks a concrete message type per role."""
    request = ChatCompletionRequest.model_validate(
        minimal_request(
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ]
        )
    )

    assert [message.role for message in request.messages] == ["system", "user"]


def test_multimodal_user_content_is_parsed_into_parts() -> None:
    """A user message may mix text and images."""
    request = ChatCompletionRequest.model_validate(
        minimal_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                    ],
                }
            ]
        )
    )

    message = request.messages[0]
    assert isinstance(message, UserMessage)
    assert isinstance(message.content, list)
    image = message.content[1]
    assert isinstance(image, ImageContentPart)
    assert image.image_url.detail == "auto"


def test_unknown_content_part_type_is_reported_against_the_part() -> None:
    """An unknown part type is located by key path, with pydantic's union
    bookkeeping (the matched role tag, the branch type labels) stripped out."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(
            minimal_request(
                messages=[{"role": "user", "content": [{"type": "video", "video": "x"}]}]
            )
        )

    detail = error_response_from_validation_error(caught.value).error
    assert detail.param == "messages[0].content"
    assert "messages[0].content[0]: Input tag 'video'" in detail.message


def test_assistant_message_must_carry_something() -> None:
    """An assistant turn with no content, refusal, or tool calls is a bug."""
    with pytest.raises(ValidationError):
        AssistantMessage.model_validate({"role": "assistant"})


def test_assistant_message_with_only_tool_calls_is_valid() -> None:
    """Tool calls alone are a complete assistant turn."""
    message = AssistantMessage.model_validate(
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
    )

    assert message.content is None
    assert message.tool_calls is not None


def test_tool_result_must_answer_a_preceding_call() -> None:
    """A dangling tool_call_id names itself instead of failing upstream."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(
            minimal_request(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "tool", "content": "42", "tool_call_id": "call_missing"},
                ]
            )
        )

    assert "call_missing" in str(caught.value)


def test_tool_result_following_its_call_is_accepted() -> None:
    """The well-formed call/result pair round-trips."""
    request = ChatCompletionRequest.model_validate(
        minimal_request(
            messages=[
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Delhi"}'},
                        }
                    ],
                },
                {"role": "tool", "content": "31C", "tool_call_id": "call_1"},
            ]
        )
    )

    assert isinstance(request.messages[2], ToolMessage)


def test_duplicate_tool_names_are_rejected() -> None:
    """Two tools with one name make the model's choice ambiguous."""
    tool = {"type": "function", "function": {"name": "lookup"}}
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(minimal_request(tools=[tool, dict(tool)]))

    assert "lookup" in str(caught.value)


def test_forcing_an_undeclared_tool_is_rejected() -> None:
    """tool_choice may only name a tool the request actually declared."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(
            minimal_request(
                tools=[{"type": "function", "function": {"name": "lookup"}}],
                tool_choice={"type": "function", "function": {"name": "other"}},
            )
        )

    assert "other" in str(caught.value)


def test_tool_choice_mode_needs_no_tools() -> None:
    """The string modes stay valid on their own."""
    request = ChatCompletionRequest.model_validate(minimal_request(tool_choice="auto"))
    assert request.tool_choice == "auto"


def test_top_logprobs_requires_logprobs() -> None:
    """Asking for alternatives without asking for log probabilities is a no-op."""
    with pytest.raises(ValidationError, match="top_logprobs"):
        ChatCompletionRequest.model_validate(minimal_request(top_logprobs=3))


def test_stream_options_require_streaming() -> None:
    """stream_options on a non-streaming request would be silently ignored."""
    with pytest.raises(ValidationError, match="stream_options"):
        ChatCompletionRequest.model_validate(
            minimal_request(stream_options={"include_usage": True})
        )


def test_deprecated_max_tokens_still_resolves() -> None:
    """The old spelling keeps working and resolves to one output cap."""
    request = ChatCompletionRequest.model_validate(minimal_request(max_tokens=256))
    assert request.effective_max_tokens == 256


def test_max_completion_tokens_wins_when_both_agree() -> None:
    """Sending both spellings is fine as long as they say the same thing."""
    request = ChatCompletionRequest.model_validate(
        minimal_request(max_tokens=256, max_completion_tokens=256)
    )
    assert request.effective_max_tokens == 256


def test_conflicting_token_limits_are_rejected() -> None:
    """Two different output caps have no defensible resolution."""
    with pytest.raises(ValidationError, match="disagree"):
        ChatCompletionRequest.model_validate(
            minimal_request(max_tokens=256, max_completion_tokens=512)
        )


def test_logit_bias_keys_must_be_token_ids() -> None:
    """logit_bias is keyed by token ID; a word here silently does nothing."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(minimal_request(logit_bias={"hello": 5.0}))


def test_json_schema_response_format_uses_the_wire_key() -> None:
    """The schema is read from 'schema' and dumped back under the same key."""
    request = ChatCompletionRequest.model_validate(
        minimal_request(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": {"type": "object"}},
            }
        )
    )

    dumped = request.model_dump(by_alias=True, exclude_none=True)
    assert dumped["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_stop_sequences_are_capped() -> None:
    """OpenAI accepts at most four stop sequences."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(minimal_request(stop=["a", "b", "c", "d", "e"]))


def test_response_serialises_in_openai_shape() -> None:
    """A response dumps to the exact envelope an OpenAI client expects."""
    response = ChatCompletionResponse(
        model="gpt-4o-mini",
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ResponseMessage(content="hi"),
                finish_reason="stop",
            )
        ],
        usage=TokenUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
    )

    dumped = response.model_dump(exclude_none=True)
    assert dumped["object"] == "chat.completion"
    assert dumped["id"].startswith("chatcmpl-")
    assert dumped["created"] > 0
    assert dumped["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert dumped["usage"]["total_tokens"] == 10
    assert "vortex" not in dumped


def test_response_carries_optional_gateway_provenance() -> None:
    """The additive 'vortex' block records who actually served the request."""
    response = ChatCompletionResponse.model_validate(
        {
            "model": "claude-opus-5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "vortex": {"provider": "anthropic", "upstream_model": "claude-opus-5"},
        }
    )

    assert response.vortex is not None
    assert response.vortex.provider == "anthropic"


def test_response_requires_at_least_one_choice() -> None:
    """A completion with no choices is not a completion."""
    with pytest.raises(ValidationError):
        ChatCompletionResponse(model="gpt-4o-mini", choices=[])


def test_unknown_finish_reason_is_rejected() -> None:
    """finish_reason is a closed set; adapters must map into it."""
    with pytest.raises(ValidationError):
        ChatCompletionChoice.model_validate(
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "end_turn",
            }
        )


def test_chunk_serialises_as_a_streaming_delta() -> None:
    """A streamed chunk carries deltas and its own object type."""
    chunk = ChatCompletionChunk.model_validate(
        {
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "he"}}],
        }
    )

    dumped = chunk.model_dump(exclude_none=True)
    assert dumped["object"] == "chat.completion.chunk"
    assert dumped["choices"][0]["delta"]["content"] == "he"
    assert dumped["choices"][0].get("finish_reason") is None


def test_model_list_matches_openai_discovery_shape() -> None:
    """GET /v1/models answers with a list envelope of model cards."""
    listing = ModelList(data=[ModelCard(id="fast", owned_by="openai")])

    dumped = listing.model_dump()
    assert dumped["object"] == "list"
    assert dumped["data"][0] == {
        "id": "fast",
        "object": "model",
        "created": 0,
        "owned_by": "openai",
    }


def test_format_parameter_path_drops_transport_and_union_tags() -> None:
    """The reported path names only keys the caller actually sent."""
    assert format_parameter_path(("body", "messages", 0, "user", "content")) == (
        "messages[0].content"
    )
    assert format_parameter_path(("body",)) is None


def test_format_parameter_path_keeps_a_field_named_after_its_own_tag() -> None:
    """`text` the tag is dropped; `text` the field survives."""
    location = (
        "body",
        "messages",
        0,
        "user",
        "content",
        "list[tagged-union[X]]",
        0,
        "text",
        "text",
    )
    assert format_parameter_path(location) == "messages[0].content[0].text"


def test_nested_field_error_reports_the_full_key_path() -> None:
    """A bad value deep inside a tool declaration names the exact key."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(
            minimal_request(tools=[{"type": "function", "function": {"name": "bad name!"}}])
        )

    assert error_response_from_validation_error(caught.value).error.param == (
        "tools[0].function.name"
    )


def test_response_format_schema_error_names_the_wire_key() -> None:
    """The `json_schema` tag collapses into the `json_schema` field."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(
            minimal_request(response_format={"type": "json_schema", "json_schema": {"name": "x"}})
        )

    assert error_response_from_validation_error(caught.value).error.param == (
        "response_format.json_schema.schema"
    )


def test_error_envelope_reports_every_failure_but_one_param() -> None:
    """All failures are listed; param/code describe the first."""
    with pytest.raises(ValidationError) as caught:
        ChatCompletionRequest.model_validate(minimal_request(temperature=9.0, n=0))

    envelope = error_response_from_validation_error(caught.value)
    assert envelope.error.type == "invalid_request_error"
    assert "temperature" in envelope.error.message
    assert "n:" in envelope.error.message
    assert envelope.error.param == "temperature"


def test_error_envelope_handles_an_empty_error_list() -> None:
    """A failure with no field-level detail still produces a valid envelope."""
    envelope = error_response_from_validation_error([])
    assert envelope.error.message == "Invalid request."
    assert envelope.error.param is None


def test_error_response_rejects_an_unknown_error_type() -> None:
    """The error taxonomy is closed, so clients can branch on it."""
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate({"error": {"message": "x", "type": "kaboom"}})
