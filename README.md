# vortex-ai-gateway

**v0.3**

## What is This?

vortex-ai-gateway is an AI Gateway designed to serve as a unified control plane and routing hub for AI model interactions. It manages request flow, handles authentication, orchestrates model selection, and provides a centralized entry point for AI applications.

## About the Name

**Vortex** — A vortex represents a center of concentrated activity and convergence. In fluid dynamics, a vortex is where multiple flows merge into a cohesive center. We chose this name because a gateway should act as a convergence point—drawing together multiple AI requests, models, and services, then directing them intelligently through a unified system.

**AI Gateway** — Self-explanatory. A gateway in networking and systems design controls and directs traffic between different domains. An AI Gateway specifically manages traffic and interactions with artificial intelligence systems.

Together, **vortex-ai-gateway** evokes both the convergence point metaphor and the explicit purpose of the system.

## Getting Started

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.
Python 3.14 is pinned in `.python-version`.

### Installation

```bash
# Creates the virtualenv, installs the project and dev tooling from uv.lock
uv sync
```

### Configuration

Settings load from environment variables prefixed `VORTEX_` (see
`vortex_ai_gateway.config.Settings`). For local development, copy the template
and edit as needed — `.env` is git-ignored and read automatically:

```bash
cp .env.example .env
```

Every setting has a default, so the app also runs with no `.env` at all. A real
environment variable always overrides a line in `.env`.

### Running the Application

```bash
uv run uvicorn vortex_ai_gateway.gateway:app --reload
```

The server will be available at `http://localhost:8000`

### Logging

Logs are emitted as one JSON object per line on stdout (structlog); pipe through
`jq` in development. Every request is assigned a request ID — taken from an
inbound `X-Request-ID` header or generated — which is attached to every log line
for that request, returned in the `X-Request-ID` response header, and available
to handlers as `request.state.request_id`. `VORTEX_LOG_LEVEL` sets the
threshold.

### Development

```bash
uv run pytest            # tests with coverage
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run mypy src          # strict type check

uv run pre-commit install    # enable hooks on commit
uv run pre-commit run --all-files
```

## Project Layout

```
src/vortex_ai_gateway/    # the package (src/ layout, not flat)
tests/                    # imports the installed package, never src/
```

The `src/` layout is deliberate. Tests import `vortex_ai_gateway` from the
installed distribution rather than from the working directory, so a packaging
mistake — a module missing from the wheel, a bad `pyproject.toml` — fails the
test run instead of being masked by Python finding the source tree first.
CI enforces this by installing a built wheel (`uv sync --no-editable`), and
the pytest config deliberately sets no `pythonpath`.

## CI/CD

Every push and pull request runs, in order:

| Stage | Command |
|-------|---------|
| Install | `uv sync --locked --no-editable` |
| Lint | `ruff check src tests` |
| Format | `ruff format --check src tests` |
| Types | `mypy src` (strict) |
| Tests | `pytest -v` |

`--locked` fails the build if `uv.lock` is out of step with `pyproject.toml`,
so dependency changes cannot land without a matching lockfile update.

The `main` branch requires these checks to pass and a pull request review.

## License

Licensed under the Apache License 2.0. See LICENSE file for details.
