"""Tests for the stub API-key authentication at the edge."""

import pytest
from fastapi.testclient import TestClient

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.providers import MockProvider

CHAT_URL = "/v1/chat/completions"

BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}


@pytest.fixture
def provider() -> MockProvider:
    """A provider that must never be reached by an unauthenticated request."""
    return MockProvider()


def build_client(provider: MockProvider, api_keys: str = "") -> TestClient:
    """A client with no default credentials, so each test supplies its own."""
    settings = Settings(_env_file=None, api_keys=api_keys)
    return TestClient(create_app(settings=settings, provider=provider))


def test_missing_header_is_rejected(provider: MockProvider) -> None:
    """No credentials at all is a 401 in the same envelope as everything else."""
    response = build_client(provider).post(CHAT_URL, json=BODY)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "missing_api_key"
    assert "Bearer" in error["message"]
    assert provider.call_count == 0


def test_unauthenticated_response_advertises_the_scheme(provider: MockProvider) -> None:
    """A 401 without WWW-Authenticate leaves the client guessing."""
    response = build_client(provider).post(CHAT_URL, json=BODY)

    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ("Token abc123", "invalid_authorization_header"),
        ("abc123", "invalid_authorization_header"),
        ("Bearer ", "missing_api_key"),
        ("", "missing_api_key"),
    ],
)
def test_malformed_headers_are_distinguished(
    provider: MockProvider, header: str, code: str
) -> None:
    """A missing key and an unreadable header are different bugs at the caller."""
    response = build_client(provider).post(CHAT_URL, json=BODY, headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == code


def test_any_well_formed_key_passes_when_none_are_configured(provider: MockProvider) -> None:
    """The development default: no allow-list, but a key is still required."""
    response = build_client(provider).post(
        CHAT_URL, json=BODY, headers={"Authorization": "Bearer anything"}
    )

    assert response.status_code == 200
    assert provider.call_count == 1


def test_configured_allow_list_is_enforced(provider: MockProvider) -> None:
    """With keys configured, an unknown key is rejected."""
    client = build_client(provider, api_keys="key-one, key-two")

    accepted = client.post(CHAT_URL, json=BODY, headers={"Authorization": "Bearer key-two"})
    rejected = client.post(CHAT_URL, json=BODY, headers={"Authorization": "Bearer key-three"})

    assert accepted.status_code == 200
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_api_key"
    assert provider.call_count == 1


def test_authentication_precedes_validation(provider: MockProvider) -> None:
    """An unauthenticated caller learns nothing about its payload being wrong."""
    response = build_client(provider).post(CHAT_URL, json={"model": ""})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_health_probes_stay_open(provider: MockProvider) -> None:
    """Probes must not need credentials, or the orchestrator kills the pod."""
    client = build_client(provider, api_keys="key-one")

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_configured_keys_are_parsed_from_one_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators type a comma-separated list, not JSON."""
    monkeypatch.setenv("VORTEX_API_KEYS", " key-one , key-two ,, ")
    settings = Settings(_env_file=None)

    assert settings.allowed_api_keys == frozenset({"key-one", "key-two"})


def test_no_configured_keys_means_an_empty_allow_list() -> None:
    """An unset value is an empty set, not a set containing the empty string."""
    assert Settings(_env_file=None, api_keys="").allowed_api_keys == frozenset()
