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

### Provider routing

Which vendor serves which model is one ordered, comma-separated table. First
match wins, so a specific rule may precede a general one, and patterns are
shell globs:

```bash
VORTEX_MODEL_ROUTES=gpt-4o=openai,gpt-*=openai,claude-*=anthropic,local/*=ollama
VORTEX_OPENAI_API_KEY=sk-...
VORTEX_ANTHROPIC_API_KEY=sk-ant-...
```

The table is also the enable list. Only the providers it names are constructed;
one that is named without its key stops the gateway at startup rather than on
the first request that routes there; and a model no rule claims is rejected with
a `404`, unless `VORTEX_DEFAULT_PROVIDER` says where to send it. Leave the table
empty — the default — and the gateway answers from its built-in mock provider,
loudly, so a keyless checkout still works end to end.

Callers keep using the OpenAI wire format throughout: the model name in the
request body is what selects the provider, and the response names the one that
served it under `vortex.provider`.

Two timeouts, not one: `VORTEX_REQUEST_TIMEOUT_SECONDS` (default 30) budgets the
*answer*, and `VORTEX_CONNECT_TIMEOUT_SECONDS` (default 5) budgets the
connection. They are separate because sharing a number lets an unreachable host
hold a worker for the full generation window — measured in
[`docs/notes/day-04.md`](docs/notes/day-04.md).

### Streaming

Set `"stream": true` and the reply is server-sent events in OpenAI's framing —
`data: {chunk}` per event, terminated by `data: [DONE]`. `examples/stream.sh`
renders the tokens as they land:

```bash
uv run uvicorn vortex_ai_gateway.gateway:app | tee gateway.log   # in one shell
examples/stream.sh "write a haiku about latency"                 # in another
examples/stream.sh --abandon 2                                   # hang up mid-stream
```

Two things happen behind that stream that a plain relay would not do.

**Every stream is accounted for, whether or not the caller asked.** Token counts
arrive in a final chunk providers only send when `stream_options.include_usage`
was set, so the gateway always sets it and strips the chunk back out when the
caller did not ask for it. The caller sees exactly the OpenAI-compatible stream
they expect; the gateway still gets the bill, as one log line per request:

```json
{"event": "stream finished", "outcome": "completed", "provider": "openai",
 "model": "fake-model", "upstream_model": "fake-model", "chunks": 13,
 "prompt_tokens": 7, "completion_tokens": 12, "total_tokens": 19}
```

**Hanging up stops the meter.** When a client disconnects mid-generation the
cancellation is carried into the provider's stream, which closes the upstream
connection rather than leaving it generating tokens nobody will read. The
request is recorded as `"outcome": "abandoned"` at warning level — an access log
cannot tell that case from a stream that simply finished quickly, and it is the
one that costs money with nothing to show for it. See ADR-018 and ADR-019.

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

## Resilience

Every provider gets its own retry loop and its own circuit breaker, so one
vendor's outage cannot stop another's traffic. Failures are classified by the
taxonomy in `providers/errors.py`: transient ones are retried with full jitter
inside a wall-clock deadline, a `429` waits exactly as long as the provider
asked, and a `400` is never retried and never counts against the breaker.

```bash
VORTEX_MODEL_ROUTES=gpt-*=openai,claude-*=anthropic
VORTEX_FALLBACK_CHAINS=openai>anthropic     # hop when openai's breaker is open
VORTEX_RETRY_MAX_ATTEMPTS=3
VORTEX_RETRY_DEADLINE_SECONDS=90            # must exceed the request timeout
VORTEX_BREAKER_FAILURE_THRESHOLD=5          # failed requests, not attempts
```

Retries, breaker transitions and fallback hops each emit one JSON log event
carrying the request ID, so `event:"provider fallback"` is a single query. See
ADR-001, ADR-002 and ADR-020 for the decisions behind the defaults.

## API keys

Keys live hashed in SQLite and are minted by a CLI, not an endpoint — creating
the *first* credential over an authenticated API is a bootstrapping problem
whose usual answer is a bootstrap secret, which is the plaintext key we were
trying to get rid of (ADR-003).

```bash
export VORTEX_KEY_DB_PATH=/var/lib/vortex/keys.sqlite3

uv run vortex-keys create --name ci-pipeline --rpm 600 --tpm 150000
# vtx_9f2c41ab77de_qtZ8...  ← shown once; only the SHA-256 is stored
uv run vortex-keys list
uv run vortex-keys revoke 9f2c41ab77de     # effective on the next request
```

A token is `vtx_<key_id>_<secret>`. The `key_id` is public: it is what you
revoke by, what appears in logs, and what a rate limit and a bill hang off. The
secret is 32 CSPRNG bytes hashed with SHA-256 — not bcrypt, because a work
factor exists to make *guessable* secrets expensive to guess and there is no
dictionary behind 256 random bits, so it would buy nothing and cost ~100 ms of
CPU on every request.

With `VORTEX_KEY_DB_PATH` set, the plaintext `VORTEX_API_KEYS` list is ignored
rather than merged: two allow-lists means revoking from one and still being let
in by the other.

## Rate limits and usage

Each key gets a requests-per-minute and a tokens-per-minute allowance, held as
token buckets in Redis and checked in a single Lua script — a request refused by
the token bucket must not have already spent a request (ADR-021).

```bash
VORTEX_METERING_ENABLED=true
VORTEX_REDIS_URL=redis://localhost:6379/0
VORTEX_RATE_LIMIT_DEFAULT_RPM=60            # for keys with no limit of their own
VORTEX_RATE_LIMIT_DEFAULT_TPM=90000         # 0 means unlimited
VORTEX_RATE_LIMIT_ASSUMED_COMPLETION_TOKENS=512
```

Every response carries its remaining allowance, not just the rejections, so a
client can slow down *before* it is throttled:

```
x-ratelimit-limit-requests: 60      x-ratelimit-limit-tokens: 90000
x-ratelimit-remaining-requests: 59  x-ratelimit-remaining-tokens: 89498
x-ratelimit-reset-requests: 1.0s    x-ratelimit-reset-tokens: 0.3s
```

A rejection is a `429` with `Retry-After` and `"code": "gateway_rate_limit_exceeded"`,
which is how it is told apart from a `429` relayed from a vendor — one means a
client is sending too much, the other means our own account is throttled.

Because a request's token cost is unknown until the provider answers, admission
reserves an estimate and settlement reconciles it against real usage. Redis
being unreachable **fails open** with one warning, and Redis is deliberately not
a readiness check: draining a working instance because the limiter is degraded
would turn a degradation into an outage.

`GET /v1/usage` reports the calling key's own spend, priced from a table you can
override without a release:

```bash
curl -H "Authorization: Bearer $KEY" localhost:8000/v1/usage?days=7
```

```json
{"object": "usage.report", "key_id": "9f2c41ab77de",
 "total_requests": 5, "total_unmetered_requests": 0, "total_tokens": 35,
 "total_cost_usd": "0.0000165", "unpriced_models": [], "daily": [...]}
```

The ledger stores **token counts**, never money: `HINCRBY` is exact, and pricing
at read time means a corrected price corrects the history rather than needing a
migration. Costs come back as JSON *strings* so they survive a round trip
through a parser that would otherwise make them floats. A model the table cannot
price reports `null`, never zero, and names itself in `unpriced_models` — and a
request whose usage never arrived (an abandoned stream) is counted as
*unmetered* rather than as free. `VORTEX_PRICE_TABLE_PATH` points at
`{"gpt-4o": {"prompt": 2.5, "completion": 10.0}}` in USD per million tokens; it
is consulted before the built-in table, so it need only name what it corrects.
See ADR-022.
