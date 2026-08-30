"""Tests for gateway module."""

import pytest
from fastapi.testclient import starlette.testclient.TestClient

from src.gateway import app


@pytest.fixture
def client():
    """Create test client."""
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
