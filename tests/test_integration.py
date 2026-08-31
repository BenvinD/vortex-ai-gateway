"""End-to-end integration test: drive the real app over ASGI with httpx.

Unlike ``fastapi.testclient.TestClient`` (which runs a sync portal around the
app), this exercises the app on the running event loop through
``httpx.AsyncClient`` + ``ASGITransport`` — the same async path production uses.
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app


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
