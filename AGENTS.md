# AGENTS.md

Guidance for any coding agent working in this repository. This is the canonical
file; `CLAUDE.md` imports it and adds the Claude Code-specific tooling notes
(ADR-023).

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

uv run vortex-keys create --name ci --rpm 600 --tpm 150000   # mint a client key
uv run vortex-keys list                  # every key, newest first
uv run vortex-keys revoke <key_id>       # takes effect on the next request
```

`vortex-keys` is a `[project.scripts]` entry point, so it exists only after
`uv sync`. It reads `VORTEX_KEY_DB_PATH` (or `--db`) and never talks to the
running gateway.

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
only place a provider failure becomes a status code. `streaming.py` owns what a
stream *costs*: it asks every provider for usage regardless of what the caller
requested, strips the usage chunk back out when the caller did not ask
(ADR-018), and writes one `stream finished` record per streamed request —
including the abandoned ones, where the client hung up and the cancellation was
carried into the provider's generator to close the upstream (ADR-019).

`resilience.py` holds provider-agnostic primitives — retry policy, token-bucket
retry budget, circuit breaker — and `providers/resilience_wrapper.py` composes
them through the `ChatProvider` seam: `ResilientProvider` wraps one adapter with
retries and a breaker of its own, `FallbackProvider` tries an ordered chain.
`build_router` wraps every adapter before the router sees it, so
`router.providers` holds wrappers, not adapters — reach the adapter through
`.inner` (ADR-001, ADR-002, ADR-020).

`auth.py` answers *who is calling* and returns a `Principal`, never a token:
`key_id` is public, stable, and safe to put in a Redis key or a log line, which
matters because everything downstream is keyed on it. Keys come from exactly one
of three sources — a `keys.py` SQLite store (`VORTEX_KEY_DB_PATH`, hashed and
revocable, minted by `vortex-keys`), the plaintext `VORTEX_API_KEYS` list, or
development mode — never merged, because two allow-lists means revoking from one
and still being let in by the other (ADR-003).

`ratelimit.py`, `pricing.py` and `spend.py` are the metering subsystem, and
`metering.py` is the only one `routes.py` talks to: `admit` before the provider
is touched, then `settle` (the request produced tokens, possibly a number nobody
has) or `discard` (it produced nothing). The limiter is a per-key RPM/TPM token
bucket in one Lua script, because a request refused by the token bucket must not
have already spent a request; the ledger stores integer token counts and prices
them at read time, so a corrected price corrects the history (ADR-021, ADR-022).
Both hang off one Redis connection and are absent together, controlled by
`VORTEX_METERING_ENABLED` — off by default, so a local run needs no Redis. Both
fail *open*, and Redis is deliberately not a readiness check: draining a working
instance because the limiter is degraded turns a degradation into an outage.

Still to come (per `pyproject.toml` and the ADR index): PII guardrails and
Redis-backed load balancing. Breaker state is still per worker process.

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
  pre-emptive sections make mypy report unused-section errors on every run. The
  same goes for stub packages: `types-redis` was removed because redis-py ships
  `py.typed` and the stubs described the 4.x API, so mypy was checking a Redis
  that has not existed for three major versions.
- `fakeredis[lua]` is a dev dependency so the limiter's tests execute the real
  Lua script through lupa. A hand-written Redis double would provide atomicity
  for free and prove nothing about the thing under test.
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
