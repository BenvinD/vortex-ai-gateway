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

    # How long to wait for an upstream provider's *answer*, in seconds. Long,
    # because generating tokens is slow.
    request_timeout_seconds: float = 30.0

    # How long to wait for the TCP/TLS handshake, in seconds. Deliberately much
    # shorter: a host that has not accepted a connection in a few seconds is
    # unreachable, not busy, and sharing the read budget with it means an
    # unroutable IP pins a worker for the whole generation window
    # (docs/notes/day-04.md).
    connect_timeout_seconds: float = 5.0

    # Client API keys accepted at the edge, comma-separated. Kept as a plain
    # string rather than a set because pydantic-settings parses collection
    # types from env vars as JSON, which is a hostile format for an operator
    # typing a value into a deployment console.
    api_keys: str = ""

    # Which provider serves which model, as an ordered, comma-separated list of
    # `pattern=provider` rules — e.g.
    #     gpt-4o=openai,claude-*=anthropic,local/*=ollama
    # First match wins, so a specific rule may precede a general one. Parsed by
    # `vortex_ai_gateway.routing`, which also owns the pattern syntax; this
    # stays a plain string for the same reason `api_keys` does. The table is
    # also the enable list: a provider no rule mentions is never constructed,
    # and an empty table leaves the gateway on its mock (ADR-016, ADR-017).
    model_routes: str = ""

    # Where a model that matches no rule goes. Empty means "reject it", which
    # is the safer default: a typo'd model name is a 400 rather than a
    # surprise bill on whichever provider happened to be listed first.
    default_provider: str = ""

    # Per-provider credentials and endpoints. An empty base URL means "use the
    # vendor's own", so only self-hosted or proxied deployments set one.
    # `routing.build_router` finds these by name, so a fourth provider needs a
    # matching `<name>_api_key` / `<name>_base_url` pair and nothing else.
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    ollama_api_key: str = ""
    ollama_base_url: str = ""

    # Resilience settings (per-provider)
    retry_max_attempts: int = 3
    retry_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 10.0

    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 60.0
    breaker_backoff_multiplier: float = 2.0
    breaker_max_open_seconds: float = 600.0

    # Optional retry budget settings (0 disables the budget)
    retry_budget_capacity: int = 0
    retry_budget_refill_per_second: float = 0.0

    @property
    def allowed_api_keys(self) -> frozenset[str]:
        """The accepted client keys, empty when the gateway is left open.

        An empty set means *no key is checked beyond being present* — the
        development default. Populate ``VORTEX_API_KEYS`` to enforce a real
        allow-list.
        """
        return frozenset(key.strip() for key in self.api_keys.split(",") if key.strip())


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once.

    Cached so it can be used as a FastAPI dependency (``Depends(get_settings)``)
    without re-parsing the environment on every request. Call
    ``get_settings.cache_clear()`` in tests that need a fresh read.
    """
    return Settings()
