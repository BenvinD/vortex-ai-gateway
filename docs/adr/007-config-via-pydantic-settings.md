# ADR-007: Configuration via pydantic-settings from the environment
Date / Status: 2026-08-31 / accepted
Context: The gateway needs per-deployment config (Redis URL, timeouts, later
provider keys) that must not live in the repo, must be validated at startup,
and must be overridable in local dev without editing tracked files.
Options: A) plain `os.environ` reads scattered at use sites B) a `pydantic-settings`
`Settings` model, env vars prefixed `VORTEX_`, optional git-ignored `.env` for
local, `.env.example` committed C) a YAML/TOML config file per environment.
Decision: B. One typed `Settings` with defaults so the app boots bare in dev;
`create_app(settings=...)` injects it (tests pass `_env_file=None`); `get_settings()`
is `lru_cache`d for use as a FastAPI dependency. Real env vars beat `.env`.
Consequences: Every setting is declared and validated in one place; a bad value
fails fast at boot. `.env` must never be committed (enforced in `.gitignore`).
Revisit if config outgrows a flat namespace or needs live reload.
