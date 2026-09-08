"""Tests for the request-ID middleware."""

import json

import pytest
import structlog
from fastapi import Request
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.middleware import RequestIDMiddleware


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
