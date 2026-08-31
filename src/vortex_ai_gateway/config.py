"""Application configuration, loaded from the environment.

Values come from environment variables prefixed ``VORTEX_`` (e.g.
``VORTEX_REDIS_URL``). For local development an optional ``.env`` file in the
project root is read as a lower-priority source; a real environment variable
always wins over a line in ``.env``.

``.env`` holds machine-local secrets and is never committed — ``.env.example``
lists the available keys with safe placeholder values.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]


class Settings(BaseSettings):
    """Runtime configuration for the gateway.

    Every field has a default so the app boots with no configuration at all in
    local development. Deployments override via environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="VORTEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    log_level: str = "INFO"

    # Redis backs rate limiting and load balancing (see the pyproject scope).
    redis_url: str = "redis://localhost:6379/0"

    # HTTP timeout, in seconds, for calls out to upstream model providers.
    request_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once.

    Cached so it can be used as a FastAPI dependency (``Depends(get_settings)``)
    without re-parsing the environment on every request. Call
    ``get_settings.cache_clear()`` in tests that need a fresh read.
    """
    return Settings()
