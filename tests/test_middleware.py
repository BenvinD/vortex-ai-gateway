"""Tests for the request-ID middleware."""

import json
from typing import Any

import pytest
import structlog
from fakeredis import aioredis
from fastapi import Request
from fastapi.testclient import TestClient

from tests.test_cache import CHAT_URL, body, build, client_for
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.middleware import RequestIDMiddleware
from vortex_ai_gateway.providers import MockProvider


def _client() -> TestClient:
    return TestClient(create_app(settings=Settings(_env_file=None)))


def test_response_carries_a_generated_request_id() -> None:
    response = _client().get("/healthz")

    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32  # uuid4().hex


def test_inbound_request_id_is_preserved() -> None:
    response = _client().get("/healthz", headers={"X-Request-ID": "trace-abc-123"})

    assert response.headers["x-request-id"] == "trace-abc-123"


def test_each_request_gets_a_distinct_id() -> None:
    client = _client()

    first = client.get("/healthz").headers["x-request-id"]
    second = client.get("/healthz").headers["x-request-id"]

    assert first != second


def test_request_id_reaches_handler_state_and_log_context() -> None:
    app = create_app(settings=Settings(_env_file=None))

    @app.get("/_probe")
    async def _probe(request: Request) -> dict[str, object]:
        return {
            "state": request.state.request_id,
            "ctx": structlog.contextvars.get_contextvars().get("request_id"),
        }

    body = TestClient(app).get("/_probe", headers={"X-Request-ID": "rid-42"}).json()

    assert body == {"state": "rid-42", "ctx": "rid-42"}


def test_request_completion_is_logged_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _client().get("/healthz", headers={"X-Request-ID": "log-me"})

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    completed = [r for r in records if r.get("event") == "request completed"]
    assert len(completed) == 1
    assert completed[0]["http_status"] == 200
    assert completed[0]["http_path"] == "/healthz"
    assert completed[0]["request_id"] == "log-me"
    assert isinstance(completed[0]["duration_ms"], (int, float))


def test_handler_error_is_logged_then_reraised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(settings=Settings(_env_file=None))

    @app.get("/_boom")
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        TestClient(app).get("/_boom", headers={"X-Request-ID": "err-1"})

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    failed = [r for r in records if r.get("event") == "request failed"]
    assert len(failed) == 1
    assert failed[0]["request_id"] == "err-1"
    assert failed[0]["level"] == "error"


async def test_non_http_scopes_pass_straight_through() -> None:
    seen: list[str] = []

    async def downstream(scope: object, receive: object, send: object) -> None:
        assert isinstance(scope, dict)
        seen.append(scope["type"])

    middleware = RequestIDMiddleware(downstream)
    await middleware({"type": "lifespan"}, _noop, _noop)

    assert seen == ["lifespan"]
    assert structlog.contextvars.get_contextvars() == {}


async def _noop() -> None:
    return None


# --- the cache outcome on the access line -------------------------------------


def _completed(captured: str) -> list[dict[str, Any]]:
    """The ``request completed`` records in a captured stdout, in order."""
    records = [json.loads(line) for line in captured.splitlines() if line.strip()]
    return [record for record in records if record.get("event") == "request completed"]


async def test_a_miss_and_a_hit_are_distinguishable_in_the_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same request twice logs two lines that say which one was free.

    Before the ``cache`` field the two were identical but for ``duration_ms``,
    so a hit rate could not be computed from the logs at all — the counters
    that could are per-process and die with the worker.
    """
    async with client_for(build(aioredis.FakeRedis(), MockProvider())) as client:
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body())

    assert [record["cache"] for record in _completed(capsys.readouterr().out)] == ["MISS", "HIT"]


async def test_a_streamed_request_is_logged_as_a_bypass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stream is not cached, and the line says so rather than saying ``MISS``.

    The streaming ending is the one that reaches this middleware differently:
    the response starts before the body exists, so the header has to be read at
    ``http.response.start`` and not from a completed response.
    """
    async with client_for(build(aioredis.FakeRedis(), MockProvider())) as client:
        await client.post(CHAT_URL, json=body(stream=True))

    assert [record["cache"] for record in _completed(capsys.readouterr().out)] == ["BYPASS"]


async def test_a_gateway_with_no_cache_omits_the_field_rather_than_logging_null(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No cache configured, no ``cache`` key — nulls would poison the arithmetic."""
    async with client_for(build(None, MockProvider())) as client:
        await client.post(CHAT_URL, json=body())

    completed = _completed(capsys.readouterr().out)
    assert len(completed) == 1
    assert "cache" not in completed[0]


async def test_the_hit_rate_is_recoverable_from_the_log_lines_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The checkpoint, stated as the query an operator would actually run.

    Three lookups and one bypass: the bypass must not land in either total, or
    a deployment that streams heavily would report a hit rate that falls as the
    cache works exactly as designed.
    """
    async with client_for(build(aioredis.FakeRedis(), MockProvider())) as client:
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body())
        await client.post(CHAT_URL, json=body(stream=True))

    outcomes = [record["cache"] for record in _completed(capsys.readouterr().out)]
    hits = outcomes.count("HIT")
    lookups = hits + outcomes.count("MISS")

    assert outcomes == ["MISS", "HIT", "HIT", "BYPASS"]
    assert hits / lookups == pytest.approx(2 / 3)
