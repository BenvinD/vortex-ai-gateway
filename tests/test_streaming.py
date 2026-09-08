"""Tests for stream accounting: who gets the bill, and who pays for a hang-up.

The disconnect tests drive the app as raw ASGI rather than through
``TestClient``. A client hanging up is an ``http.disconnect`` message, and the
only way to send one on cue — after a specific chunk, not whenever a test
client happens to tear its transport down — is to be the server.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceDelta,
    GatewayMetadata,
)
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import MockProvider
from vortex_ai_gateway.streaming import STREAM_EVENT, StreamRecord, metered, wants_usage

CHAT_URL = "/v1/chat/completions"
AUTH = {"Authorization": "Bearer test-key"}


def body(**overrides: object) -> dict[str, object]:
    """A minimal streaming request body, plus any overrides."""
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
    } | overrides


def client_for(provider: object) -> TestClient:
    """An app serving ``provider``, isolated from the ambient environment."""
    return TestClient(
        create_app(settings=Settings(_env_file=None), provider=provider), headers=AUTH
    )


def chunks_from(response: Any) -> list[dict[str, Any]]:
    """Every JSON chunk in an SSE body, sentinel excluded."""
    events = [line for line in response.iter_lines() if line.startswith("data: ")]
    return [json.loads(event.removeprefix("data: ")) for event in events if event != "data: [DONE]"]


def records(captured: str, event: str) -> list[dict[str, Any]]:
    """The structured log lines for one event name."""
    lines = [json.loads(line) for line in captured.splitlines() if line.strip()]
    return [line for line in lines if line.get("event") == event]


class EndlessProvider:
    """A provider that streams until something stops it, and says how it ended.

    The point of the disconnect tests is that *something stops it*: with the
    teardown broken, a test awaiting this provider's stream never returns.
    """

    name = "endless"

    def __init__(self) -> None:
        self.closed = False
        self.tokens = 0

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        raise NotImplementedError("this provider only streams")

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        try:
            while True:
                self.tokens += 1
                yield ChatCompletionChunk(
                    model=request.model,
                    choices=[
                        ChatCompletionChunkChoice(
                            index=0, delta=ChoiceDelta(content=f"tok{self.tokens} ")
                        )
                    ],
                )
                await asyncio.sleep(0.01)
        finally:
            # Reached when the generator is unwound, however that happens. In a
            # real adapter this is where the upstream connection is closed.
            self.closed = True


def scope_for(payload: bytes) -> dict[str, Any]:
    """An ASGI scope for one streaming POST, as uvicorn would build it."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": CHAT_URL,
        "raw_path": CHAT_URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway.test"),
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer test-key"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("gateway.test", 80),
    }


async def stream_until_disconnect(app: Any, payload: bytes, *, after: int) -> list[bytes]:
    """Read the stream, then hang up after ``after`` body chunks have arrived."""
    delivered: list[bytes] = []
    hung_up = asyncio.Event()
    inbound: list[dict[str, Any]] = [{"type": "http.request", "body": payload, "more_body": False}]

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            delivered.append(message.get("body", b""))
            if len(delivered) >= after:
                hung_up.set()

    # Bounded, because "the upstream is never torn down" shows up as a hang.
    await asyncio.wait_for(app(scope_for(payload), receive, send), timeout=5)
    return delivered


# -- Metering ---------------------------------------------------------------


def test_wants_usage_reads_what_the_caller_asked_for() -> None:
    asked = ChatCompletionRequest.model_validate(body(stream_options={"include_usage": True}))
    silent = ChatCompletionRequest.model_validate(body())

    assert wants_usage(asked) is True
    assert wants_usage(silent) is False


def test_metering_turns_usage_on_without_touching_the_original() -> None:
    """The caller's request keeps saying what the caller actually asked for."""
    request = ChatCompletionRequest.model_validate(body())

    metered_request = metered(request)

    assert wants_usage(metered_request) is True
    assert request.stream_options is None


def test_metering_leaves_a_request_that_already_asked_alone() -> None:
    request = ChatCompletionRequest.model_validate(body(stream_options={"include_usage": True}))

    assert metered(request) is request


def test_usage_is_requested_from_the_provider_even_when_the_caller_did_not() -> None:
    """The gateway is the one being billed, so it always asks for the bill."""
    provider = MockProvider()

    with client_for(provider).stream("POST", CHAT_URL, json=body()) as response:
        response.read()

    (received,) = provider.received_requests
    assert received.stream_options is not None
    assert received.stream_options.include_usage is True


def test_the_usage_chunk_is_withheld_from_a_caller_who_did_not_ask() -> None:
    """Asking upstream for usage must not change what an OpenAI client sees."""
    with client_for(MockProvider()).stream("POST", CHAT_URL, json=body()) as response:
        chunks = chunks_from(response)

    assert chunks, "the stream should still carry deltas"
    assert all("usage" not in chunk for chunk in chunks)
    assert all(chunk["choices"] for chunk in chunks), "no empty carrier chunk leaks out"


def test_the_usage_chunk_is_forwarded_to_a_caller_who_asked() -> None:
    request = body(stream_options={"include_usage": True})

    with client_for(MockProvider()).stream("POST", CHAT_URL, json=request) as response:
        chunks = chunks_from(response)

    final = chunks[-1]
    assert final["choices"] == []
    assert final["usage"]["total_tokens"] > 0


# -- The record -------------------------------------------------------------


def test_the_bill_is_logged_even_when_the_caller_never_asked_for_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point: a request with no usage chunk still has a usage record."""
    with client_for(MockProvider()).stream("POST", CHAT_URL, json=body()) as response:
        response.read()

    (record,) = records(capsys.readouterr().out, STREAM_EVENT)
    assert record["outcome"] == "completed"
    assert record["provider"] == "mock"
    assert record["model"] == "gpt-4o-mini"
    assert record["finish_reason"] == "stop"
    assert record["total_tokens"] == record["prompt_tokens"] + record["completion_tokens"]
    assert record["chunks"] > 0


def test_a_failed_stream_is_still_accounted_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = MockProvider(replies=[RuntimeError("connection reset")])

    with client_for(provider).stream("POST", CHAT_URL, json=body()) as response:
        response.read()

    (record,) = records(capsys.readouterr().out, STREAM_EVENT)
    assert record["outcome"] == "failed"
    assert record["level"] == "warning"


def test_a_record_reports_the_provider_s_own_count_not_the_relayed_deltas() -> None:
    """Usage is carried as reported: an edge count cannot see prompt tokens."""
    record = StreamRecord(provider="mock", model="gpt-4o-mini")
    chunk = ChatCompletionChunk(
        model="gpt-4o-mini",
        choices=[],
        usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},  # type: ignore[arg-type]
    )

    assert record.observe(chunk, forward_usage=False) is None
    assert record.usage is not None
    assert record.usage.total_tokens == 14


def test_usage_riding_alongside_deltas_loses_only_the_usage() -> None:
    """A provider may attach usage to a chunk that also carries content."""
    record = StreamRecord(provider="mock", model="gpt-4o-mini")
    chunk = ChatCompletionChunk(
        model="gpt-4o-mini",
        choices=[ChatCompletionChunkChoice(index=0, delta=ChoiceDelta(content="hi"))],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},  # type: ignore[arg-type]
    )

    forwarded = record.observe(chunk, forward_usage=False)

    assert forwarded is not None
    assert forwarded.usage is None
    assert forwarded.choices[0].delta.content == "hi"
    assert record.usage is not None and record.usage.total_tokens == 2


# -- Hanging up -------------------------------------------------------------


async def test_a_client_disconnect_tears_down_the_provider_stream() -> None:
    """Ctrl-C on the client must stop the tokens the gateway is paying for."""
    provider = EndlessProvider()
    app = create_app(settings=Settings(_env_file=None), provider=provider)

    delivered = await stream_until_disconnect(app, json.dumps(body()).encode(), after=3)

    assert provider.closed, "the provider stream was left open, still generating"
    assert 0 < provider.tokens < 50, "generation continued long after the client left"
    assert delivered, "some tokens reached the client before it hung up"


async def test_an_abandoned_stream_is_recorded_as_abandoned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An access log cannot tell this from a short success. This record can."""
    app = create_app(settings=Settings(_env_file=None), provider=EndlessProvider())

    await stream_until_disconnect(app, json.dumps(body()).encode(), after=3)
    structlog.contextvars.clear_contextvars()

    (record,) = records(capsys.readouterr().out, STREAM_EVENT)
    assert record["outcome"] == "abandoned"
    assert record["level"] == "warning"
    assert record["provider"] == "endless"
    assert record["chunks"] > 0


def test_the_record_names_the_adapter_that_served_it_not_the_seam() -> None:
    """A routed deployment mounts a router; the invoice comes from the vendor."""
    record = StreamRecord(provider="router", model="fake-model")
    chunk = ChatCompletionChunk(
        model="fake-model",
        choices=[ChatCompletionChunkChoice(index=0, delta=ChoiceDelta(content="hi"))],
        vortex=GatewayMetadata(provider="openai", upstream_model="gpt-4o-mini-2024-07-18"),
    )

    record.observe(chunk, forward_usage=False)

    assert record.provider == "openai"
    assert record.upstream_model == "gpt-4o-mini-2024-07-18"
