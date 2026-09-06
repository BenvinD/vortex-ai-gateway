"""Tests for the stub API-key authentication at the edge."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vortex_ai_gateway.auth import Principal, _from_record, _from_token
from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.gateway import create_app
from vortex_ai_gateway.keys import KeyStore, fingerprint
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


# --- keys from the store (ADR-003) -------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "keys.sqlite3")


def build_store_client(provider: MockProvider, store: KeyStore, **overrides: object) -> TestClient:
    """A client whose keys come from the database, not the environment."""
    settings = Settings(_env_file=None, key_db_path=str(store.path), **overrides)
    return TestClient(create_app(settings=settings, provider=provider))


def test_a_stored_key_is_accepted(provider: MockProvider, store: KeyStore) -> None:
    minted = store.create(name="ci")

    response = build_store_client(provider, store).post(
        CHAT_URL, json=BODY, headers={"Authorization": f"Bearer {minted.token}"}
    )

    assert response.status_code == 200
    assert provider.call_count == 1


def test_a_revoked_key_stops_working_without_a_redeploy(
    provider: MockProvider, store: KeyStore
) -> None:
    """The whole point of the store: revocation takes effect on the next request."""
    minted = store.create(name="leaked")
    client = build_store_client(provider, store)
    assert (
        client.post(
            CHAT_URL, json=BODY, headers={"Authorization": f"Bearer {minted.token}"}
        ).status_code
        == 200
    )

    store.revoke(minted.record.key_id)

    rejected = client.post(CHAT_URL, json=BODY, headers={"Authorization": f"Bearer {minted.token}"})
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_api_key"
    assert provider.call_count == 1


def test_a_store_supersedes_the_environment_list(provider: MockProvider, store: KeyStore) -> None:
    """Two allow-lists means revoking from one and being let in by the other."""
    client = build_store_client(provider, store, api_keys="legacy-key")

    response = client.post(CHAT_URL, json=BODY, headers={"Authorization": "Bearer legacy-key"})

    assert response.status_code == 401
    assert provider.call_count == 0


def test_an_unknown_key_and_a_wrong_secret_are_told_apart_only_in_the_logs(
    provider: MockProvider, store: KeyStore
) -> None:
    """The 401 is identical; telling them apart tells an attacker which IDs exist."""
    minted = store.create()
    client = build_store_client(provider, store)
    forged = f"vtx_{minted.record.key_id}_wrong-secret"

    unknown = client.post(
        CHAT_URL, json=BODY, headers={"Authorization": "Bearer vtx_000000000000_x"}
    )
    wrong = client.post(CHAT_URL, json=BODY, headers={"Authorization": f"Bearer {forged}"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_the_principal_carries_the_keys_own_limits(store: KeyStore) -> None:
    """A per-key limit beats the deployment default, and zero falls through to it."""
    capped = store.create(name="batch", rpm=600)
    uncapped = store.create(name="default")
    settings = Settings(_env_file=None, rate_limit_default_rpm=60, rate_limit_default_tpm=90_000)

    capped_record = store.verify(capped.token)
    uncapped_record = store.verify(uncapped.token)
    assert capped_record is not None and uncapped_record is not None

    assert _from_record(capped_record, settings) == Principal(
        key_id=capped.record.key_id, name="batch", rpm=600, tpm=90_000
    )
    assert _from_record(uncapped_record, settings) == Principal(
        key_id=uncapped.record.key_id, name="default", rpm=60, tpm=90_000
    )


def test_an_environment_key_is_identified_without_being_stored() -> None:
    """A key with no record still needs something safe to meter and bill against."""
    settings = Settings(_env_file=None, api_keys="plaintext-key", rate_limit_default_rpm=60)

    principal = _from_token("plaintext-key", settings)

    assert principal == Principal(key_id=fingerprint("plaintext-key"), rpm=60)
    assert "plaintext-key" not in principal.key_id


def test_a_principal_with_no_limits_is_not_metered() -> None:
    """Zero is what lets a gateway with no rate limits never open a Redis connection."""
    assert Principal(key_id="abc").metered is False
    assert Principal(key_id="abc", rpm=1).metered is True
    assert Principal(key_id="abc", tpm=1).metered is True
