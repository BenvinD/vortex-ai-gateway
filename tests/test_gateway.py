"""Tests for gateway module."""

import pytest
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app


@pytest.fixture
def client() -> TestClient:
    """Create test client with settings isolated from the ambient environment."""
    app = create_app(settings=Settings(_env_file=None))
    return TestClient(app)


def test_create_app_stores_injected_settings() -> None:
    """The factory keeps the passed-in settings on ``app.state``."""
    settings = Settings(_env_file=None, environment="prod")
    app = create_app(settings=settings)
    assert app.state.settings is settings


async def _noop() -> None:
    """A readiness check that always passes."""


def test_healthz_is_always_ok(client: TestClient) -> None:
    """Liveness probe returns 200 without consulting any dependency."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_checks_pass() -> None:
    """Readiness probe returns 200 and an "ok" per registered check."""
    app = create_app()
    app.state.readiness_checks["redis"] = _noop
    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["redis"] == "ok"


def test_readyz_returns_503_when_a_check_fails() -> None:
    """A failing dependency check drains the instance with a 503 + breakdown."""
    app = create_app()

    async def boom() -> None:
        raise RuntimeError("redis unreachable")

    app.state.readiness_checks["redis"] = boom
    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert "redis unreachable" in body["redis"]
