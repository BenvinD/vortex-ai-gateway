# vortex-ai-gateway

**v0.3**

## What is This?

vortex-ai-gateway is an AI Gateway designed to serve as a unified control plane and routing hub for AI model interactions. It manages request flow, handles authentication, orchestrates model selection, and provides a centralized entry point for AI applications.

[`docs/architecture.md`](docs/architecture.md) walks one request through every
layer — auth, limits, cache, routing, resilience, adapter — with the decision
behind each and the condition under which it was the wrong one.

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
scripts/                  # one-off experiments and load generators
docs/design|adr|notes/    # the paper trail: before, decided, after
deploy/                   # Prometheus config, Grafana provisioning, the dashboard JSON
compose.yaml, Dockerfile  # the reference stack (see Observability)
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

## Response cache

Two tiers, cheap one first. The **exact** tier is a SHA-256 over the validated
request (sorted keys, minus the fields that cannot change a generated token),
namespaced per API key so one tenant's completion is never served to another;
a hit is a Redis `GET` and is settled at zero tokens. The **semantic** tier is
asked only when the exact tier *looked and missed* — never when it bypassed —
because an embedding is a network call and a hash is not. Only `messages` is
embedded; everything else is hashed into the namespace the vector is searched
in, so the threshold is about wording alone. A semantic hit is promoted into
the exact tier, so the next identical request costs a hash, not an embedding.
Streams bypass both. See ADR-004 and ADR-024.

```bash
VORTEX_CACHE_ENABLED=true
VORTEX_CACHE_TTL_SECONDS=300
VORTEX_CACHE_TTLS=/v1/chat/completions=600   # per route; 0 means never cache
VORTEX_CACHE_SCOPE=key                       # or global, single-tenant only
VORTEX_SEMANTIC_CACHE_ENABLED=false          # see below for why
VORTEX_SEMANTIC_CACHE_THRESHOLD=0.95
VORTEX_EMBEDDING_MODEL=                      # an Ollama model name; empty = no embedder
VORTEX_EMBEDDING_BASE_URL=                   # falls back to VORTEX_OLLAMA_BASE_URL
```

Every response says which tier answered — `X-Cache: HIT|MISS|BYPASS`, and
`X-Semantic-Cache` with `X-Semantic-Cache-Score` carrying the nearest cosine
on misses too. That score distribution is the evidence for moving the
threshold. `X-Vortex-Cache-Bypass: true` on a request skips lookup *and* store,
so debugging the cache cannot change what is in it.

### Why the semantic tier is off by default

The threshold was measured, not guessed: `scripts/threshold_experiment.py`
embeds 20 paraphrase pairs (should HIT) and 20 hard near-miss pairs — one
changed number, a swapped unit, an antonym — that must MISS, and picks the
threshold under one rule: **no near miss may clear it**. With both local
embedders tried, no threshold does.

![Cosine similarity of prompt pairs, nomic-embed-text: no safe threshold](docs/notes/threshold-nomic-embed-text.png)

![Cosine similarity of prompt pairs, mxbai-embed-large: no safe threshold](docs/notes/threshold-mxbai-embed-large.png)

| model | paraphrase min / median | near-miss median / **max** | at 0.95: false hits / false misses | safe threshold |
|---|---|---|---|---|
| `nomic-embed-text` | 0.838 / 0.948 | 0.893 / **0.996** | 2/20 / 10/20 | none |
| `mxbai-embed-large` | 0.879 / 0.952 | 0.903 / **0.986** | 1/20 / 8/20 | none |

The worst near miss — *Convert 5 miles to kilometres* vs *Convert 5 kilometres
to miles*, 0.996 — outscores the best genuine paraphrase. Cosine over a
sentence embedding measures what a text is *about*, and those two are about
the same thing; a cache serves answers, and theirs differ. So the tier ships
as tested plumbing with a documented reason not to enable it (ADR-005).
Enabling it is a procedure, not a flag: pull a model, run the script, set the
threshold it prints, and re-run on every model change.

```bash
ollama pull nomic-embed-text
uv run python scripts/threshold_experiment.py --model nomic-embed-text --url http://localhost:11434
VORTEX_SEMANTIC_CACHE_ENABLED=true VORTEX_EMBEDDING_MODEL=nomic-embed-text \
  uv run uvicorn vortex_ai_gateway.gateway:app
```

The embedder is `embedding.py`, its own seam rather than a chat adapter
(ADR-025). With it on, the two-tier path reads back in the headers:

```
"What's the capital of France?"   X-Cache: MISS  X-Semantic-Cache: MISS
"capital city of France?"         X-Cache: MISS  X-Semantic-Cache: HIT   X-Semantic-Cache-Score: 0.9658
"capital city of France?"         X-Cache: HIT   X-Semantic-Cache: BYPASS
```

— and so does the false hit the plot predicts: *Convert 5 kilometres to miles*
is served the answer to *Convert 5 miles to kilometres* at 0.9955.

Raw scores are in `docs/notes/threshold-<model>.json`; the write-up is
`docs/notes/day-10.md`.

## Observability

Three things, in one stack: a trace per request, a Prometheus endpoint, and a
dashboard committed as a file.

```bash
docker compose up -d                       # gateway + redis + prometheus + grafana
uv run python scripts/generate_traffic.py  # put something on it
open http://localhost:3000                 # Grafana, anonymous, dashboard already there
```

| | |
|---|---|
| Gateway | <http://localhost:8000/metrics> |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3000> — dashboard **Vortex / Vortex AI Gateway** |
| Jaeger | <http://localhost:16686> — `VORTEX_TRACING_ENABLED=true docker compose --profile tracing up -d` |

Nothing in that stack is a production default: Grafana has authentication off,
Redis has no password, and the gateway runs on its mock provider so the whole
thing comes up with no vendor keys and no bill.

### Metrics

`/metrics` is served **open**, beside `/healthz` and `/readyz` rather than under
`/v1`, because a scraper has no API key and issuing it one puts a credential
that can read your traffic volumes into a monitoring system's config file
(ADR-028). It publishes counts, latencies and totals — never a prompt, a
completion, or a key. A gateway exposed directly to the internet should set
`VORTEX_METRICS_ENABLED=false` and scrape a sidecar.

| Metric | What it answers |
|---|---|
| `vortex_requests_total{route,method,status,model,provider}` | traffic, error rate, model mix |
| `vortex_request_duration_seconds` | latency quantiles, by route and model |
| `vortex_stream_ttft_seconds` | time to first token — streams only |
| `vortex_cache_lookups_total{tier,outcome}` | hit rate per tier, bypasses excluded |
| `vortex_provider_attempts_total{provider,outcome}` | upstream calls per request served |
| `vortex_provider_retries_total`, `..._give_ups_total{reason}` | the retry storm, and how it ended |
| `vortex_fallbacks_total`, `vortex_breaker_transitions_total`, `vortex_breaker_state` | what failed over, and what is refusing traffic now |
| `vortex_streams_total{outcome}` | completed / failed / **abandoned** |
| `vortex_tokens_total{model,kind}`, `vortex_cost_usd_total{model}` | what it cost |

The numbers are per **worker process**, like the circuit breaker's state and
both caches' counters. Run one worker per port and scale with containers.

**`model` is capped.** It comes out of a request body, and a Prometheus label
value allocates a series that lives until the process exits — so the gateway
admits `VORTEX_METRICS_LABEL_BUDGET` distinct values (50 by default) and reports
everything after that as `other` (ADR-027). Seeing `other` on a dashboard means
raise the budget. `route` is the matched route *template*, never the path as
sent, so a scanner guessing URLs allocates one series and not one per guess.

### Tracing

Off by default, and off costs nothing rather than little: with no SDK provider
installed the OpenTelemetry API returns a no-op tracer, so the instrumentation
at the seams stays in the code path and does nothing.

```bash
VORTEX_TRACING_ENABLED=true VORTEX_TRACING_EXPORTER=console \
  uv run uvicorn vortex_ai_gateway.gateway:app
```

One trace per request, branching at the four things worth timing separately.
This is a real trace from a gateway pointed at a dead upstream, with
`VORTEX_RETRY_MAX_ATTEMPTS=3`:

```
POST /v1/chat/completions            ERROR  status=502  model=gpt-4o-mini  provider=router
├─ cache.exact.lookup                       tier=exact     outcome=off
├─ cache.semantic.lookup                    tier=semantic  outcome=off
└─ provider.complete                 ERROR  provider=router  model=gpt-4o-mini
   ├─ provider.attempt               ERROR  attempt=1 of 3
   ├─ provider.attempt               ERROR  attempt=2 of 3
   └─ provider.attempt               ERROR  attempt=3 of 3
```

That is the retry storm as a shape rather than as three log lines nobody
joined. Every log line the request emits carries `trace_id` and `span_id`, so a
line copied out of the logs pastes straight into Jaeger:

```json
{"event": "request completed", "http_status": 502, "duration_ms": 349.1,
 "request_id": "1072374a1ca4473f815e7e1803a2402a",
 "trace_id": "2197d057837046a1545a96477e00b693", "span_id": "79c00601d3bbee17"}
```

Spans are written by hand rather than installed by
`opentelemetry-instrumentation-fastapi`, because that installs itself in the
`BaseHTTPMiddleware` shape this codebase already refuses — it breaks streaming,
and streaming with a correct ending taxonomy is the most carefully built thing
here (ADR-026).

### Generating traffic

`scripts/generate_traffic.py` sends a deliberate *mix* — repeats so the cache
has something to hit, several models so the breakdowns have series, streams so
TTFT has a source, and a few bad requests so the error panel is a working error
panel — then reads the result back out of `/metrics`. A real run, 600 requests
against the mock provider with Redis and the exact cache on:

```
  sent 600 requests in 1.3s (477/s)

  client saw
    kind      buffered=416, bypass=38, malformed=9, stream=110, unroutable=27
    status    200=591, 400=9
    X-Cache   BYPASS=148, HIT=410, MISS=33, none=9
    latency   p50=25ms p95=71ms

  gateway published
    requests  600 total, 600 on the chat route
    cache     410 hits / 443 lookups = 92.6%, and 148 bypasses that are in neither
    tokens    2671
    cost      $0.002598
    streams   110, 110 with a TTFT observation
```

The 148 bypasses are the 110 streams (which bypass the cache in both
directions, ADR-004) plus the 38 requests that asked to. They are in neither
half of the hit rate, which is the point: the cache was never asked, so it
cannot have missed.

And the resilience counters from the dead-upstream gateway above, after four
requests with `VORTEX_BREAKER_FAILURE_THRESHOLD=2`:

```
vortex_provider_attempts_total{outcome="error",provider="openai"}          6.0
vortex_provider_retries_total{error="ProviderUnavailable",...}             4.0
vortex_provider_give_ups_total{provider="openai",reason="attempts exhausted"} 2.0
vortex_breaker_transitions_total{provider="openai",state="open"}           1.0
vortex_breaker_state{provider="openai"}                                    2.0
vortex_requests_total{...,status="502"}                                    2.0
vortex_requests_total{...,status="503"}                                    2.0
```

Six upstream calls for the first two requests — three attempts each — and then
zero for the next two, because the breaker opened and the 503s cost nothing
(ADR-002). `sum(rate(vortex_provider_attempts_total)) / sum(rate(vortex_requests_total))`
is that story as one line on the dashboard.

### The dashboard

`deploy/grafana/dashboards/vortex-gateway.json` is provisioned into Grafana on
startup and UI edits are disabled, so the dashboard is a file that shows up in a
diff rather than a page in somebody's browser. Eighteen panels; the six that
carry the most:

| Panel | Query |
|---|---|
| Error rate | `sum(rate(vortex_requests_total{status=~"5.."}[$__rate_interval]))` over the total |
| Exact cache hit rate | `HIT` over `HIT + MISS`, with `BYPASS` out of the denominator |
| p95 chat latency | `histogram_quantile(0.95, sum by (le) (rate(vortex_request_duration_seconds_bucket{route="/v1/chat/completions"}[...])))` |
| Upstream calls per request | `sum(rate(vortex_provider_attempts_total[...])) / sum(rate(vortex_requests_total{route="/v1/chat/completions"}[...]))` |
| Breaker state | `vortex_breaker_state`, mapped 0 → closed, 1 → half-open, 2 → **OPEN** |
| Spend / hour | `sum(rate(vortex_cost_usd_total[...])) * 3600` |

> **No screenshot yet.** Docker is not installed on the machine this was built
> on, so the stack has never been stood up end to end. What *is* verified runs
> in CI: every `vortex_*` metric the eighteen panels query exists on a live
> gateway, and the datasource UID they reference is the one `deploy/` pins —
> which is the failure that would otherwise be found by a person reading "No
> data" on one panel out of eighteen. What is **not** verified is everything
> that needs a container to run: the `Dockerfile` has never been built, and
> Prometheus has never scraped anything. The gateway numbers above are real and
> were measured natively against Redis and uvicorn. On a machine with Docker:
>
> ```bash
> docker compose up -d
> uv run python scripts/generate_traffic.py --requests 2000 --delay 0.05
> open http://localhost:3000   # screenshot it, and replace this note
> ```
