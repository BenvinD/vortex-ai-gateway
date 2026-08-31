"""Tests for configuration loading.

Every ``Settings`` here is built with ``_env_file=None`` so a developer's real
``.env`` cannot influence the result; environment is controlled via monkeypatch.
"""

import pytest
from pydantic import ValidationError

from vortex_ai_gateway.config import Settings, get_settings


def test_defaults_apply_with_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VORTEX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VORTEX_REDIS_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.request_timeout_seconds == 30.0


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VORTEX_ENVIRONMENT", "prod")
    monkeypatch.setenv("VORTEX_REDIS_URL", "redis://cache.internal:6379/1")
    monkeypatch.setenv("VORTEX_REQUEST_TIMEOUT_SECONDS", "5")

    settings = Settings(_env_file=None)

    assert settings.environment == "prod"
    assert settings.redis_url == "redis://cache.internal:6379/1"
    assert settings.request_timeout_seconds == 5.0


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VORTEX_ENVIRONMENT", "production")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
