# Postman

A Postman collection for the gateway's whole HTTP surface, with assertions, and
five environments. Everything runs on the built-in mock provider, so there are
**no vendor keys and no bill** — which is what makes it demo-able on a laptop.

```
postman/
  vortex-ai-gateway.postman_collection.json   33 requests, 8 folders
  environments/*.postman_environment.json     5 environments
  CURL.md                                     the same 33 requests as curl
  build_collection.py                          generates all of the above
```

## Load it into the app

**Postman → Import → Files**, then select:

```
postman/vortex-ai-gateway.postman_collection.json
postman/environments/local.postman_environment.json
postman/environments/docker-demo.postman_environment.json
postman/environments/local-semantic.postman_environment.json
postman/environments/staging.postman_environment.json
postman/environments/production.postman_environment.json
```

Select all six in one go — the app imports a collection and five environments
together. Then pick an environment from the selector at the top right. Nothing
works until you do: the collection's pre-request script throws a named error
rather than letting thirty requests fail with a connection reset.

To get it into a cloud workspace instead, so it syncs across machines:

```bash
postman login                      # or: postman login --with-api-key <key>
postman collection migrate postman/vortex-ai-gateway.postman_collection.json
postman workspace push
```

## Environments

| Environment | Start it with | Metering | Exact cache | Semantic |
|---|---|---|---|---|
| **Local (mock, no Redis)** | see below | off | off | off |
| **Docker Demo (dev)** | `make demo` | on | on | off |
| **Local + semantic cache** | see below | on | on | **on** |
| **Staging** | your host | on | on | off |
| **Production** | your host | on | on | off |

Staging and Production carry placeholder hosts and an empty `api_key`. They are
there so the collection is shaped for a real deployment; fill in `base_url` and
`api_key` before running either.

### Local (mock, no Redis)

```bash
VORTEX_MODEL_ROUTES= VORTEX_METERING_ENABLED=false VORTEX_CACHE_ENABLED=false \
  uv run uvicorn vortex_ai_gateway.gateway:app --reload
```

The three `VORTEX_*` overrides are **not** decoration. `config.py` reads `.env`
as a lower-priority source, so a `.env` in the project root silently decides
which provider answers — and a real environment variable is how you win over
it. Without them a `.env` carrying `VORTEX_MODEL_ROUTES` turns every
`gpt-4o-mini` request into a 404.

### Docker Demo (dev)

```bash
make demo        # gateway + redis + prometheus + grafana + jaeger, with traffic
make demo-down   # and drop its volumes
```

The compose file sets `VORTEX_MODEL_ROUTES: ""` inside the container and does
not mount `.env`, so this one is the mock by construction. This is the
environment with the most assertions live: metering and the exact cache are
both on.

### Local + semantic cache

The newest tier (ADR-024, ADR-025), and the one worth showing. It needs Redis
and an embedder:

```bash
docker compose up -d redis

VORTEX_MODEL_ROUTES= \
VORTEX_METERING_ENABLED=true VORTEX_CACHE_ENABLED=true \
VORTEX_SEMANTIC_CACHE_ENABLED=true \
VORTEX_EMBEDDING_MODEL=nomic-embed-text \
VORTEX_EMBEDDING_BASE_URL=http://localhost:11434 \
VORTEX_RATE_LIMIT_DEFAULT_RPM=600 VORTEX_RATE_LIMIT_DEFAULT_TPM=150000 \
  uv run uvicorn vortex_ai_gateway.gateway:app
```

`nomic-embed-text` is one of the two embedding models already pulled in the
local Ollama. There is deliberately no default model — a threshold measured for
one model is only a prior for the next.

## Run it from the command line

The same files the app imports are what the CLI executes:

```bash
postman collection run postman/vortex-ai-gateway.postman_collection.json \
  -e postman/environments/docker-demo.postman_environment.json
```

Measured on the three environments that can be stood up locally:

| Environment | Requests | Assertions | Failures |
|---|---|---|---|
| Local (mock) | 33 | 187 | 0 |
| Docker Demo | 33 | 194 | 0 |
| Local + semantic | 33 | 197 | 0 |

The count differs because the assertions are **gated** on `metering_enabled`,
`cache_enabled`, `semantic_cache_enabled` and `routes_configured`. A test that
cannot tell a Redis-less laptop from the compose stack is a test that has to be
disabled before a demo, so instead each one asserts the behaviour its
environment actually has — including the *absence* of a header when a tier is
off, which is a claim worth making.

## Run order matters in one place

Folder **04** depends on its own order: request 1 seeds the cache entry that 2,
3 and 4 are about. Folder **05** is the same, for the vector index. Both
regenerate a nonce per run, so `MISS` means missed and not *ran twice*.

Run the collection top to bottom and folder 06's ledger assertions will have
traffic to report — `/v1/usage` is asserted to be non-empty when metering is on.

## What the folders show

| Folder | The point |
|---|---|
| 00 · Health & observability | the probe split, and why `/metrics` is open (ADR-028) |
| 01 · Authentication | one key source, never merged; identical rejection for every failure (ADR-003) |
| 02 · Chat completions | OpenAI's wire format, and `extra="forbid"` (ADR-009, ADR-010) |
| 03 · Streaming | usage is always bought, only conditionally forwarded (ADR-018) |
| 04 · Exact response cache | HIT, MISS, BYPASS, and the four fields that cannot change the key (ADR-004) |
| 05 · Semantic cache | the score on a miss, which is the evidence ADR-005 is waiting on |
| 06 · Rate limits & ledger | the `X-RateLimit-*` sextet on success, not just rejection (ADR-021, ADR-022) |
| 07 · Errors | one envelope, and `param` naming the offending field (ADR-010, ADR-015) |

## What this collection cannot show

Three things, and all three have a driver in `bench/`:

* **Abandoned streams** (ADR-019) — Postman always reads to the end, so the
  cancellation path is unreachable from here. `bench/streaming.js`.
* **Retries, the circuit breaker, fallback chains** (ADR-001, ADR-002,
  ADR-020) — they need a provider that fails on demand.
  `bench/provider-failure.js`.
* **A 429** — the demo key is 600 rpm, so one request will not reach it. Mint a
  deliberately small key instead:
  `uv run vortex-keys create --name demo-429 --rpm 2 --tpm 500`.

## Regenerating

The collection, the five environments and `CURL.md` all come out of one script,
so the assertions and the curl commands cannot drift from each other:

```bash
uv run python postman/build_collection.py
```

Edit `build_collection.py`, never the generated JSON. The script's docstring
lists which module each fact is read from — the header names, the status codes,
and the four non-semantic cache fields are all taken from the code rather than
remembered, because every one of them has moved at least once.
