# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependencies are managed with `uv`; Python 3.14 is pinned in `.python-version`.
`uv sync` installs the project plus the `dev` dependency-group from `uv.lock`.

```bash
uv sync                                  # create/refresh .venv, install project + dev tooling
uv run uvicorn vortex_ai_gateway.gateway:app --reload   # run the API on :8000

uv run pytest                            # tests (coverage is on by default via addopts)
uv run pytest tests/test_gateway.py::test_health_check   # a single test
uv run ruff check src tests              # lint
uv run ruff format src tests             # format
uv run mypy src                          # strict type check (tests/ is excluded)

uv run pre-commit install                # enable hooks
uv run pre-commit run --all-files
```

CI runs the same steps in order — `uv sync --locked --no-editable`, ruff check,
ruff format --check, mypy src, pytest -v — with `uv run --no-sync` so nothing
re-resolves mid-job. `--locked` fails if `uv.lock` is out of step with
`pyproject.toml`: any dependency edit must be followed by `uv lock` (or
`uv sync`) and the updated lockfile committed in the same change.

## Architecture

The package is a FastAPI app in `src/vortex_ai_gateway/`. `gateway.py` exposes
an app factory, `create_app()`, with the module-level `app = create_app()` used
only as the uvicorn entrypoint. Tests construct their own instance via
`create_app()` rather than importing `app`, so keep configuration inside the
factory rather than at module scope.

`contracts/` defines the unified, OpenAI-shaped wire format and depends on
nothing else. `providers/` holds the `ChatProvider` seam, a scripted
`MockProvider`, and one adapter per vendor (OpenAI, Anthropic, Ollama), each
translating that contract to and from its own format. `routing.py` maps model
names onto providers from config and is itself a `ChatProvider`, so `create_app`
mounts a router exactly where it would mount one adapter; with no routing table
configured it falls back to the mock. `routes.py` owns the HTTP surface and the
only place a provider failure becomes a status code.

Still to come (per `pyproject.toml` and the ADR index): retries and circuit
breaking, PII guardrails, rate limiting, and Redis-backed load balancing.
`redis` is a declared dependency and still unused.

### The src/ layout is load-bearing

Tests must import `vortex_ai_gateway` from the *installed* distribution, never
from the working tree. Two settings enforce this and should not be "fixed":

- `[tool.pytest.ini_options]` deliberately sets no `pythonpath`. Adding one
  would put `src/` on `sys.path` and let a broken wheel pass the test run.
- CI installs with `--no-editable`, so a module missing from the wheel or a bad
  `pyproject.toml` fails the build instead of being masked.

Run `uv sync` after adding a new module; otherwise tests will import a stale
installed copy.

### Tooling constraints worth knowing

- mypy runs `--strict`. Add `ignore_missing_imports` overrides to
  `[tool.mypy]` only when an untyped third-party package is actually imported —
  pre-emptive sections make mypy report unused-section errors on every run.
- The pre-commit ruff/mypy hooks are `repo: local` and shell out to
  `uv run --no-sync` on purpose, so hook versions match CI. The mypy hook sets
  `pass_filenames: false`; passing individual files alongside the `src` package
  root triggers "Duplicate module named ...".
- `asyncio_mode = "auto"` is set, so async tests need no `@pytest.mark.asyncio`.

## Docs conventions

`docs/` is a deliberate paper trail, not generated output:

- `docs/design/day-NN.md` — written *before* code: what is being built, the
  2–3 alternatives, and which was chosen.
- `docs/adr/NNN-short-title.md` — one decision per ADR, 6–10 lines, from
  `000-template.md`. This repo owns the `0xx` range (`1xx` belongs to a
  separate RAG repo). Update the index table in `docs/adr/README.md` when an
  ADR lands.
- `docs/notes/day-NN.md` — daily notes; the "Broke: predicted X, observed Y,
  learned Z" line is the point of the format.

When a change embodies a non-obvious decision, add the ADR alongside it.
