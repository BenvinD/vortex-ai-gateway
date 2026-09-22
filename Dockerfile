# syntax=docker/dockerfile:1

# The gateway image. Two stages, and the build stage installs exactly what CI
# installs: `uv sync --locked --no-editable`. Same flags, same reasons —
# `--locked` fails on a lockfile that has drifted from `pyproject.toml`, and
# `--no-editable` puts a built wheel in site-packages instead of a `.pth` file
# pointing at `src/`, so a module missing from the wheel breaks the build here
# rather than on the first request in production (AGENTS.md, "The src/ layout
# is load-bearing").

# Pinned to the minor version in `.python-version`. The two stages must agree
# on both the Python minor *and* the Debian release: the runtime stage copies
# the virtualenv wholesale, and a venv built against a different libc or a
# different interpreter is a container that starts and then cannot import.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer, so editing `src/` does not re-resolve
# and re-download the world. `README.md` comes along because `pyproject.toml`
# names it as the project readme and hatchling will not build without it.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --no-editable

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.14-slim-bookworm AS runtime

# `curl` is here for one reason: the HEALTHCHECK below. Everything else the
# gateway needs is a Python package.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 vortex

WORKDIR /app
COPY --from=build --chown=vortex:vortex /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER vortex
EXPOSE 8000

# Liveness, and deliberately `/healthz` rather than `/readyz`: this answers
# "should this container be restarted", and a readiness failure means "stop
# sending it traffic", which is a different and much less drastic remedy
# (ADR-006). Docker only knows how to do the drastic one.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# One worker per container. The circuit breaker's state, both caches' counters
# and every Prometheus series in this process are per worker, so two workers
# behind one port would publish whichever of them answered the scrape. Scale by
# running more containers, which is the thing the orchestrator is for.
CMD ["uvicorn", "vortex_ai_gateway.gateway:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
