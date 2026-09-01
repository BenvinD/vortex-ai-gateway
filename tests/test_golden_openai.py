"""Golden-file tests: real OpenAI wire format, parsed by the contract models.

Each file under ``tests/golden/openai`` is one response as OpenAI returns it.
The assertion is not merely "this parses" — it is that a full round trip
preserves every key and value the provider sent. A field we forgot to model
would otherwise vanish silently on the way through the gateway, which is
exactly the failure a caller cannot diagnose.

See ``tests/golden/openai/README.md`` for how to add a capture.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    ContractModel,
    ErrorResponse,
    ModelList,
)

GOLDEN_ROOT = Path(__file__).parent / "golden" / "openai"


def _documents(directory: str, pattern: str) -> list[Path]:
    """Every golden file in one directory, sorted for a stable test order."""
    return sorted((GOLDEN_ROOT / directory).glob(pattern))


def _load_lines(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON Lines capture into one dict per streamed chunk."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def assert_nothing_lost(expected: Any, actual: Any, path: str = "$") -> None:
    """Assert every key and value in the provider's payload survived the trip.

    A one-sided comparison on purpose: the gateway may *add* keys of its own
    (the ``vortex`` provenance block), but it may never drop or change one the
    provider sent.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {type(actual).__name__}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} was dropped by the contract models"
            assert_nothing_lost(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected an array, got {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: length changed"
        for index, value in enumerate(expected):
            assert_nothing_lost(value, actual[index], f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: {expected!r} became {actual!r}"


def round_trip(model: type[ContractModel], raw: dict[str, Any]) -> ContractModel:
    """Parse a payload and assert the re-serialised form is unchanged."""
    parsed = model.model_validate(raw)
    assert_nothing_lost(raw, parsed.model_dump(mode="json", by_alias=True))
    return parsed


@pytest.mark.parametrize(
    "path", _documents("completions", "*.json"), ids=lambda path: str(path.stem)
)
def test_captured_completion_round_trips(path: Path) -> None:
    """A captured completion parses and survives re-serialisation intact."""
    round_trip(ChatCompletionResponse, json.loads(path.read_text()))


@pytest.mark.parametrize("path", _documents("chunks", "*.jsonl"), ids=lambda path: str(path.stem))
def test_captured_stream_round_trips(path: Path) -> None:
    """Every chunk of a captured stream parses and survives intact."""
    chunks = _load_lines(path)
    assert chunks, f"{path} is empty"
    for raw in chunks:
        round_trip(ChatCompletionChunk, raw)


@pytest.mark.parametrize("path", _documents("errors", "*.json"), ids=lambda path: str(path.stem))
def test_captured_error_round_trips(path: Path) -> None:
    """A captured error envelope parses into the same envelope we emit."""
    round_trip(ErrorResponse, json.loads(path.read_text()))


@pytest.mark.parametrize("path", _documents("models", "*.json"), ids=lambda path: str(path.stem))
def test_captured_model_listing_round_trips(path: Path) -> None:
    """A captured model listing parses and survives intact."""
    round_trip(ModelList, json.loads(path.read_text()))


def test_the_golden_corpus_is_not_empty() -> None:
    """A glob that quietly matches nothing would make every test above vacuous."""
    assert len(_documents("completions", "*.json")) >= 3
    assert _documents("chunks", "*.jsonl")
    assert _documents("errors", "*.json")
    assert _documents("models", "*.json")


def test_text_reply_is_read_semantically() -> None:
    """Spot-check that the values land on the fields, not merely somewhere."""
    raw = json.loads((GOLDEN_ROOT / "completions" / "text_reply.json").read_text())
    completion = ChatCompletionResponse.model_validate(raw)

    choice = completion.choices[0]
    assert choice.message.content == "The capital of France is Paris."
    assert choice.finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 21
    assert completion.system_fingerprint == "fp_34a54ae93c"


def test_tool_call_arguments_stay_an_unparsed_string() -> None:
    """`arguments` is JSON *text*; parsing it here would break partial streams."""
    raw = json.loads((GOLDEN_ROOT / "completions" / "tool_call.json").read_text())
    completion = ChatCompletionResponse.model_validate(raw)

    tool_calls = completion.choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].function.name == "get_current_weather"
    assert json.loads(tool_calls[0].function.arguments)["location"] == "Paris, France"


def test_refusal_is_distinct_from_empty_content() -> None:
    """A refusal must not be flattened into a null answer."""
    raw = json.loads((GOLDEN_ROOT / "completions" / "refusal.json").read_text())
    message = ChatCompletionResponse.model_validate(raw).choices[0].message

    assert message.content is None
    assert message.refusal is not None


def test_cached_prompt_tokens_survive() -> None:
    """Cache accounting is what makes a prompt-caching regression visible."""
    raw = json.loads((GOLDEN_ROOT / "completions" / "cached_prompt.json").read_text())
    usage = ChatCompletionResponse.model_validate(raw).usage

    assert usage is not None
    assert usage.prompt_tokens_details is not None
    assert usage.prompt_tokens_details.cached_tokens == 1920


def test_streamed_tool_call_arrives_in_fragments() -> None:
    """The captured stream proves arguments are assembled across chunks."""
    chunks = [
        ChatCompletionChunk.model_validate(raw)
        for raw in _load_lines(GOLDEN_ROOT / "chunks" / "tool_call_stream.jsonl")
    ]

    fragments = [
        call.function.arguments
        for chunk in chunks
        for choice in chunk.choices
        for call in choice.delta.tool_calls or []
        if call.function is not None and call.function.arguments is not None
    ]
    assert json.loads("".join(fragments)) == {"location": "Paris"}
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


def test_streamed_usage_arrives_in_a_choiceless_final_chunk() -> None:
    """`include_usage` puts the bill in a last chunk carrying no deltas."""
    chunks = [
        ChatCompletionChunk.model_validate(raw)
        for raw in _load_lines(GOLDEN_ROOT / "chunks" / "text_stream.jsonl")
    ]

    assert all(chunk.usage is None for chunk in chunks[:-1])
    assert chunks[-1].choices == []
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 12


def test_an_unmodelled_field_fails_loudly() -> None:
    """The cost of strictness, pinned down: a new upstream field is a hard error.

    This is ADR-010's trade in one test. When it starts failing against a real
    capture, the contract is behind OpenAI and `contracts/` needs the field —
    which is the signal we want, rather than the value being dropped in silence.
    """
    raw = json.loads((GOLDEN_ROOT / "completions" / "text_reply.json").read_text())
    raw["choices"][0]["message"]["some_future_field"] = "surprise"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ChatCompletionResponse.model_validate(raw)
