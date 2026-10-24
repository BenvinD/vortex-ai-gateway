"""The app turns request-validation failures, and bugs, into the OpenAI error envelope."""

from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.contracts import ChatCompletionRequest
from vortex_ai_gateway.gateway import create_app


def _client_with_echo_route() -> TestClient:
    """An app with one route that only exists to exercise request validation."""
    app = create_app(settings=Settings(_env_file=None))

    @app.post("/_contract_probe")
    async def probe(request: ChatCompletionRequest) -> dict[str, str]:
        return {"model": request.model}

    return TestClient(app)


def test_valid_body_passes_through() -> None:
    """A well-formed request reaches the handler untouched."""
    response = _client_with_echo_route().post(
        "/_contract_probe",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json() == {"model": "gpt-4o-mini"}


def test_invalid_body_returns_400_in_the_error_envelope() -> None:
    """OpenAI clients get a 400 and an {"error": {...}} body, not a 422."""
    response = _client_with_echo_route().post(
        "/_contract_probe",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "n": 0},
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "n"
    assert "n:" in error["message"]


def test_an_unhandled_exception_is_a_500_envelope_carrying_the_request_id() -> None:
    """Was Starlette's plain-text ``Internal Server Error`` with no way to find the log."""
    app = create_app(settings=Settings(_env_file=None))

    @app.get("/_explode")
    async def explode() -> None:
        raise RuntimeError("secret internals: sk-live-abc")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/_explode", headers={"X-Request-ID": "trace-me-123"})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-request-id"] == "trace-me-123"
    error = response.json()["error"]
    assert error["type"] == "api_error"
    assert error["code"] == "internal_error"
    assert "trace-me-123" in error["message"]
    assert "sk-live" not in response.text, "the exception's text is for the log, not the caller"
