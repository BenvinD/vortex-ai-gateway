"""Tests for gateway module."""

import pytest
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import OpenAIAdapter


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


async def test_a_provider_the_factory_built_is_closed_on_shutdown() -> None:
    """Otherwise every reload leaks the upstream connection pools."""
    settings = Settings(_env_file=None, model_routes="local/*=ollama")
    app = create_app(settings=settings)
    router = app.state.provider
    opened = [adapter.client for adapter in router.providers.values()]

    async with app.router.lifespan_context(app):
        assert not any(client.is_closed for client in opened)

    assert all(client.is_closed for client in opened)


async def test_a_provider_passed_in_belongs_to_the_caller() -> None:
    """A test's own adapter must survive the app it was lent to."""
    adapter = OpenAIAdapter(api_key="k")
    app = create_app(settings=Settings(_env_file=None), provider=adapter)
    client = adapter.client

    async with app.router.lifespan_context(app):
        pass

    assert not client.is_closed
    await adapter.aclose()
