"""End-to-end: one request through every layer the real gateway runs.

Unlike ``fastapi.testclient.TestClient`` (which runs a sync portal around the
app), this exercises the app on the running event loop through
``httpx.AsyncClient`` + ``ASGITransport`` — the same async path production uses.

The unit suites each prove one layer works. This proves the layers are *in the
right order and talking to each other*, which is the assertion no single-layer
test can make: the request ID is minted before anything can log, the principal
exists before the limiter counts against it, the limiter runs before the cache
so a hit still costs a request, and the ledger sees the same token counts the
response reported. Every double here is the repo's designated one — the mock
provider and `fakeredis[lua]` — so nothing between the ASGI scope and the Lua
script is stubbed out.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis import aioredis

from vortex_ai_gateway.cache import CACHE_HEADER
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.keys import KeyStore
from vortex_ai_gateway.providers import MockProvider
from vortex_ai_gateway.spend import SETTLED_EVENT
from vortex_ai_gateway.streaming import STREAM_EVENT

CHAT_URL = "/v1/chat/completions"
USAGE_URL = "/v1/usage"

#: The access-log line `RequestIDMiddleware` writes for every request.
ACCESS_EVENT = "request completed"


def body(**overrides: object) -> dict[str, object]:
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    } | overrides


def log_lines(captured: str) -> list[dict[str, Any]]:
    """Every structured log line emitted, as parsed JSON."""
    return [json.loads(line) for line in captured.splitlines() if line.strip().startswith("{")]


def one(lines: list[dict[str, Any]], event: str) -> dict[str, Any]:
    """The single line for ``event``, failing loudly if there is not exactly one."""
    matching = [line for line in lines if line.get("event") == event]
    assert len(matching) == 1, f"expected exactly one {event!r} line, got {len(matching)}"
    return matching[0]


@pytest.fixture
async def async_client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=Settings(_env_file=None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        yield client


async def test_healthz_round_trip_over_asgi(async_client: httpx.AsyncClient) -> None:
    response = await async_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(response.headers["x-request-id"]) == 32  # middleware-minted uuid4


class Stack:
    """A gateway with every layer live, and the handles to inspect them."""

    def __init__(self, tmp_path: Path) -> None:
        self.redis = aioredis.FakeRedis()
        self.store = KeyStore(tmp_path / "keys.sqlite3")
        self.minted = self.store.create(name="integration", rpm=10, tpm=100_000)
        self.provider = MockProvider()
        self.app = create_app(
            settings=Settings(
                _env_file=None,
                key_db_path=str(self.store.path),
                metering_enabled=True,
                cache_enabled=True,
                cache_ttl_seconds=60,
            ),
            provider=self.provider,
            redis=self.redis,
        )

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.minted.token}"}

    def client(self, *, authenticated: bool = True) -> httpx.AsyncClient:
        """A client over this app. `authenticated=False` sends no bearer token.

        Separate clients rather than a per-request header override, because
        httpx *merges* request headers into the client's defaults: passing
        `headers={}` to a call on an authenticated client sends the token
        anyway, which silently turns an auth test into a validation test.
        """
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://gateway.test",
            headers=self.auth if authenticated else None,
        )


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


async def test_one_request_through_the_whole_stack(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """Auth → limit → cache → provider → settle, asserted at every seam.

    One request, and every layer has to have run in the right order for these
    to hold together: the rate-limit headers cannot exist without a principal,
    the ledger row cannot exist without the reservation the limiter made, and
    the access line cannot carry `api_key_id` unless auth bound it before the
    handler ran.
    """
    async with stack.client() as client:
        response = await client.post(
            CHAT_URL, json=body(), headers={"X-Request-ID": "trace-me-through-the-stack"}
        )

    assert response.status_code == 200
    payload = response.json()

    # --- headers: identity, allowance, cache -----------------------------------
    assert response.headers["x-request-id"] == "trace-me-through-the-stack", (
        "an inbound request ID must survive the hop, or a trace stops at our edge"
    )
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers[CACHE_HEADER] == "MISS"
    assert response.headers["x-ratelimit-limit-requests"] == "10"
    assert response.headers["x-ratelimit-remaining-requests"] == "9"
    assert response.headers["x-ratelimit-limit-tokens"] == "100000"
    assert "x-ratelimit-reset-tokens" in response.headers
    assert "retry-after" not in response.headers, "nothing was throttled"

    # --- the provider saw the caller's request, not a rewritten one -------------
    assert stack.provider.call_count == 1
    sent = stack.provider.received_requests[0]
    assert sent.model == "gpt-4o-mini"
    assert sent.messages[0].content == "ping"

    # --- the body is the OpenAI envelope ---------------------------------------
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] > 0

    # --- logs: one access line, one settlement, both carrying the same trace ----
    lines = log_lines(capsys.readouterr().out)
    access = one(lines, ACCESS_EVENT)
    settled = one(lines, SETTLED_EVENT)

    assert access["http_status"] == 200
    assert access["http_path"] == CHAT_URL
    assert access["http_method"] == "POST"
    assert isinstance(access["duration_ms"], float)
    assert access["api_key_id"] == stack.minted.record.key_id
    assert settled["model"] == "gpt-4o-mini"
    assert settled["prompt_tokens"] == payload["usage"]["prompt_tokens"]
    assert settled["completion_tokens"] == payload["usage"]["completion_tokens"]
    assert settled["metered"] is True

    request_ids = {
        line.get("request_id") for line in lines if line["event"] != "price table loaded"
    }
    assert request_ids == {"trace-me-through-the-stack"}, (
        "every line emitted while serving a request must carry that request's ID"
    )

    # --- the token count reached the ledger, not just the log -------------------
    report = await stack.app.state.meter.ledger.report(stack.minted.record.key_id, days=1)
    assert report.total_requests == 1
    assert report.total_tokens == payload["usage"]["total_tokens"]
    assert report.total_cost_usd is not None, "gpt-4o-mini is in the built-in price table"


async def test_the_second_identical_request_is_free_but_still_counted(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cache sits below the limiter and above the provider, and it shows.

    This is the ordering assertion: a hit costs a request against the RPM
    allowance (so the cache is not a way around the limiter) and no tokens
    against the ledger (so it is not billed for a call nobody made).
    """
    async with stack.client() as client:
        first = await client.post(CHAT_URL, json=body())
        second = await client.post(CHAT_URL, json=body())

    assert [first.headers[CACHE_HEADER], second.headers[CACHE_HEADER]] == ["MISS", "HIT"]
    assert stack.provider.call_count == 1
    assert second.json() == first.json()

    # The allowance moved for both; the ledger's token count only for the first.
    assert second.headers["x-ratelimit-remaining-requests"] == "8"
    report = await stack.app.state.meter.ledger.report(stack.minted.record.key_id, days=1)
    assert report.total_requests == 2
    assert report.total_tokens == first.json()["usage"]["total_tokens"]

    # --- the metrics hooks Day 10 reads ----------------------------------------
    assert stack.app.state.cache.stats.snapshot() == {
        "hits": 1,
        "misses": 1,
        "bypasses": 0,
        "stores": 1,
        "errors": 0,
        "hit_rate": 0.5,
    }

    settlements = [
        line for line in log_lines(capsys.readouterr().out) if line["event"] == SETTLED_EVENT
    ]
    assert len(settlements) == 2, "both requests settled; only one of them cost anything"
    assert settlements[0]["prompt_tokens"] > 0
    assert (settlements[1]["prompt_tokens"], settlements[1]["completion_tokens"]) == (0, 0), (
        "a hit bought no tokens"
    )
    assert settlements[1]["metered"] is True, "and it is not an unmetered request either"


async def test_a_stream_through_the_whole_stack(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """The streaming path carries the same headers and leaves the same trail."""
    async with stack.client() as client:
        async with client.stream("POST", CHAT_URL, json=body(stream=True)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store"
            assert response.headers[CACHE_HEADER] == "BYPASS", "streams are not cached (ADR-004)"
            assert response.headers["x-ratelimit-limit-requests"] == "10"
            events = [line async for line in response.aiter_lines() if line.startswith("data: ")]

    assert events[-1] == "data: [DONE]"
    chunks = [json.loads(event.removeprefix("data: ")) for event in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert not any(chunk.get("usage") for chunk in chunks), (
        "the caller did not ask for usage, so the usage chunk is stripped (ADR-018)"
    )

    lines = log_lines(capsys.readouterr().out)
    stream = one(lines, STREAM_EVENT)
    assert stream["outcome"] == "completed"
    assert stream["chunks"] == len(chunks) + 1, (
        "the gateway observed one more chunk than it forwarded — the usage chunk it "
        "asked for on the caller's behalf and stripped back out (ADR-018)"
    )
    assert stream["total_tokens"] > 0, "usage is collected even though it was not forwarded"

    # Collected, and then actually billed: the stream's tokens reach the ledger.
    report = await stack.app.state.meter.ledger.report(stack.minted.record.key_id, days=1)
    assert report.total_tokens == stream["total_tokens"]
    assert report.total_unmetered_requests == 0


async def test_the_usage_endpoint_reports_what_the_stack_recorded(stack: Stack) -> None:
    """`GET /v1/usage` is the read side of the same ledger the write path used."""
    async with stack.client() as client:
        completion = await client.post(CHAT_URL, json=body())
        usage = await client.get(USAGE_URL, params={"days": 1})

    assert usage.status_code == 200
    report = usage.json()
    assert report["key_id"] == stack.minted.record.key_id
    assert report["total_requests"] == 1
    assert report["total_tokens"] == completion.json()["usage"]["total_tokens"]
    assert report["daily"][0]["models"][0]["model"] == "gpt-4o-mini"


async def test_an_unauthenticated_request_stops_before_every_other_layer(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """Auth is the first gate: nothing below it runs, and nothing below it logs.

    The body is deliberately invalid as well. A `401` rather than a `400` is the
    assertion — an unauthenticated caller must not be able to probe the request
    schema, and a rejected request must not reach the limiter or the provider.
    """
    async with stack.client(authenticated=False) as client:
        response = await client.post(CHAT_URL, json={"not": "a valid request"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "authentication_error"
    assert CACHE_HEADER not in response.headers, "the cache was never consulted"
    assert "x-ratelimit-limit-requests" not in response.headers, "no allowance was spent"
    assert stack.provider.call_count == 0

    lines = log_lines(capsys.readouterr().out)
    assert one(lines, ACCESS_EVENT)["http_status"] == 401
    assert not [line for line in lines if line["event"] == SETTLED_EVENT]
