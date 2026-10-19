"""Tests for the OpenAI-compatible HTTP surface."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_resilience import DiesMidStream, ScriptedProvider, policy
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import CannedReply, MockProvider
from vortex_ai_gateway.providers.errors import ProviderUnavailable
from vortex_ai_gateway.providers.resilience_wrapper import ResilientProvider
from vortex_ai_gateway.resilience import CircuitBreaker

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


def serving(provider: object) -> TestClient:
    """An app serving ``provider``, isolated from the ambient environment."""
    return TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider),  # type: ignore[arg-type]
        headers=AUTH,
    )


def test_a_stream_that_fails_before_its_first_chunk_is_a_status_code() -> None:
    """Nothing has been sent yet, so the caller gets a status, not a 200 (ADR-032).

    This was a 200 followed by an in-band error event: the route returned the
    response before the adapter had made its HTTP request at all.
    """
    client = serving(MockProvider(replies=[RuntimeError("connection reset")]))

    response = client.post(CHAT_URL, json=body(stream=True))

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["type"] == "api_error"
    assert "connection reset" in error["message"]


def test_a_stream_that_fails_after_its_first_chunk_ends_with_an_error_event() -> None:
    """The 200 went out with the first chunk, so the error has to arrive in-band."""
    client = serving(DiesMidStream())

    with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
        assert response.status_code == 200
        events = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert len(events) == 3, "the first chunk, the error, the sentinel"
    error = json.loads(events[-2].removeprefix("data: "))["error"]
    assert error["type"] == "api_error"
    assert "mid-stream" in error["message"]
    assert events[-1] == "data: [DONE]"


def test_a_stream_is_retried_before_its_200() -> None:
    """The upstream's first failure never reaches the caller, as on the buffered path."""
    flaky = ScriptedProvider(ProviderUnavailable("upstream 503", provider="primary"), None)
    client = serving(
        ResilientProvider(
            inner=flaky, policy=policy(backoff_base=0.0), breaker=CircuitBreaker(name="primary")
        )
    )

    with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
        assert response.status_code == 200
        events = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert flaky.calls == 2
    assert events[-1] == "data: [DONE]"
    assert not any('"error"' in event for event in events)


def test_a_stream_against_a_dead_upstream_is_a_502_and_then_a_503() -> None:
    """Exhausted retries, then an open breaker: two statuses a client can act on."""
    down = ScriptedProvider(ProviderUnavailable("down", provider="primary"))
    breaker = CircuitBreaker(name="primary", failure_threshold=1, open_seconds=30.0)
    client = serving(
        ResilientProvider(inner=down, policy=policy(backoff_base=0.0), breaker=breaker)
    )

    first = client.post(CHAT_URL, json=body(stream=True))
    second = client.post(CHAT_URL, json=body(stream=True))

    assert (first.status_code, second.status_code) == (502, 503)
    assert down.calls == 3, "the open breaker refused the second before anyone was called"
    assert int(second.headers["retry-after"]) >= 1


def test_app_defaults_to_the_mock_provider() -> None:
    """The endpoint is exercisable with no configuration at all."""
    app = create_app(settings=Settings(_env_file=None))

    assert isinstance(app.state.provider, MockProvider)
    assert TestClient(app, headers=AUTH).post(CHAT_URL, json=body()).status_code == 200
