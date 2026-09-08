"""Tests for the OpenAI-compatible HTTP surface."""

import json

import pytest
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import CannedReply, MockProvider

CHAT_URL = "/v1/chat/completions"

#: Any well-formed key is accepted while no allow-list is configured.
AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def provider() -> MockProvider:
    """A provider whose script each test sets up as it needs."""
    return MockProvider()


@pytest.fixture
def client(provider: MockProvider) -> TestClient:
    """An app serving the mock, isolated from the ambient environment."""
    return TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider), headers=AUTH
    )


def body(**overrides: object) -> dict[str, object]:
    """A minimal valid request body, plus any overrides."""
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    } | overrides


def test_completion_returns_the_openai_envelope(client: TestClient) -> None:
    """A buffered completion comes back in the shape an OpenAI client parses."""
    response = client.post(CHAT_URL, json=body())

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "mock reply to: ping"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] > 0


def test_absent_fields_are_omitted_not_null(client: TestClient) -> None:
    """Null-stripping keeps the payload readable and OpenAI-shaped."""
    payload = client.post(CHAT_URL, json=body()).json()

    assert "tool_calls" not in payload["choices"][0]["message"]
    assert "refusal" not in payload["choices"][0]["message"]


def test_request_reaches_the_provider_intact(client: TestClient, provider: MockProvider) -> None:
    """The router validates and forwards; it does not rewrite."""
    client.post(CHAT_URL, json=body(temperature=0.3, user="u-1"))

    assert provider.call_count == 1
    assert provider.received_requests[0].temperature == 0.3
    assert provider.received_requests[0].user == "u-1"


def test_invalid_body_is_rejected_before_the_provider(
    client: TestClient, provider: MockProvider
) -> None:
    """A bad request costs nothing: it never reaches an upstream."""
    response = client.post(CHAT_URL, json=body(temperature=9.0))

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "temperature"
    assert provider.call_count == 0


def test_provider_failure_becomes_a_502_envelope() -> None:
    """An upstream failure is reported as a gateway error, not a 500."""
    provider = MockProvider(replies=[RuntimeError("upstream on fire")])
    client = TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider), headers=AUTH
    )

    response = client.post(CHAT_URL, json=body())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "api_error"
    assert error["code"] == "RuntimeError"
    assert "upstream on fire" in error["message"]


def test_stream_is_sse_terminated_by_done(client: TestClient) -> None:
    """The stream is framed as SSE and ends with the sentinel clients await."""
    with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert events[-1] == "data: [DONE]"
    chunks = [json.loads(event.removeprefix("data: ")) for event in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)


def test_streamed_deltas_reassemble_into_the_reply() -> None:
    """Concatenating the stream reproduces exactly what the provider said."""
    provider = MockProvider(replies=[CannedReply("streamed in pieces")])
    client = TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider), headers=AUTH
    )

    with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
        events = [line for line in response.iter_lines() if line.startswith("data: ")]

    text = ""
    for event in events[:-1]:
        for choice in json.loads(event.removeprefix("data: "))["choices"]:
            text += choice["delta"].get("content", "")
    assert text == "streamed in pieces"


def test_stream_failure_is_reported_as_a_final_error_event() -> None:
    """The 200 is already sent, so the error has to arrive in-band."""
    provider = MockProvider(replies=[RuntimeError("connection reset")])
    client = TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider), headers=AUTH
    )

    with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
        assert response.status_code == 200
        events = [line for line in response.iter_lines() if line.startswith("data: ")]

    error = json.loads(events[-2].removeprefix("data: "))["error"]
    assert error["type"] == "api_error"
    assert "connection reset" in error["message"]
    assert events[-1] == "data: [DONE]"


def test_app_defaults_to_the_mock_provider() -> None:
    """The endpoint is exercisable with no configuration at all."""
    app = create_app(settings=Settings(_env_file=None))

    assert isinstance(app.state.provider, MockProvider)
    assert TestClient(app, headers=AUTH).post(CHAT_URL, json=body()).status_code == 200
