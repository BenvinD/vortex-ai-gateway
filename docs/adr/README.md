# Architecture Decision Records

One ADR per decision, 6–10 lines. Written *after* the decision, *before* moving on.
Copy `000-template.md` to `NNN-short-title.md` and fill it in.

These are the interview answers. If a decision has no ADR, it will not survive
being questioned six weeks from now.

## Index

This repo is the gateway (Gatewright), which owns the `0xx` range. Numbers are
**topical, not chronological** — slots were reserved by subject up front, so
ADR-001 is dated after ADR-019. Read the `Date` line, not the number, for
sequence.

| # | Decision | Chose | Rejected | When the rejected option wins |
|---|----------|-------|----------|-------------------------------|
| 001 | Retry policy | branch on `retryable`, full jitter, wall-clock deadline, optional budget; streams too, up to their first chunk (ADR-032) | fixed count for every failure; branch on HTTP status | A provider's `Retry-After` proves untrustworthy |
| 002 | Breaker thresholds | one breaker per provider, single half-open trial, failures counted per request | retries alone; one breaker for the whole gateway | Fleet outgrows per-worker state — then Redis, fail-open |
| 003 | Key storage | SHA-256-hashed keys in SQLite, minted by a CLI | plaintext env list; Postgres; a `POST /v1/keys` endpoint | More than one node needs the same keys — then Postgres, same schema, same hash |
| 004 | Response cache & streaming policy | exact canonical-hash cache in Redis, namespaced per `key_id`, streams bypass | hash the raw body; one global namespace; replay stored chunks | Streams dominate and repeat — then store on the *completed* ending only |
| 005 | Semantic-cache threshold, model & false-positive policy | zero false hits on a checked-in hard-near-miss set; threshold is the experiment script's output per model; no model has passed, tier off by default | cost-weighted false hits; a fixed prior threshold; a default local model | A wrong answer is cheap and reviewed by a person before use — then cost-weighted |
| 006 | Health probe semantics | `/healthz` + `/readyz` split | single dependency-checking `/health` | Service mesh owns readiness gating itself |
| 007 | Configuration source | `pydantic-settings` from env, `.env` local only | scattered `os.environ`; per-env config files | Config outgrows a flat namespace or needs live reload |
| 008 | Logging & request IDs | structlog JSON to stdout + raw-ASGI request-ID middleware | stdlib text logging; OpenTelemetry now | Need distributed traces/spans, not just correlation IDs |
| 009 | Unified request/response contract | OpenAI chat-completions shape as the internal contract | bespoke Vortex schema; lowest-common-denominator subset | Gateway stops being a drop-in OpenAI replacement |
| 010 | Request validation & errors | `extra="forbid"` + cross-field validators, OpenAI `400` error envelope | pass unknown fields through; ignore them | Gateway must proxy provider-specific extras verbatim |
| 011 | Provider seam & test doubles | narrow `ChatProvider` protocol + scripted `MockProvider` | build the OpenAI adapter first; recorded HTTP fixtures | Verifying one adapter's translation against real vendor bytes |
| 012 | Chat endpoint & stream framing | OpenAI SSE + `[DONE]`, injected provider, `502` buffered / in-band streamed errors | buffer-only now; `501` until a real adapter exists | Deploying for real, where a mock default is a liability |
| 013 | Client authentication | `Authorization: Bearer` checked in a router-level dependency, `401` + envelope | leave `/v1` open for now; delegate to a fronting API gateway | A fronting gateway or mesh already authenticates every caller |
| 014 | Vendor adapters & translation | translate both ways per vendor, refuse what a vendor cannot express | drop unsupported params silently; narrow the contract to the intersection | Callers need one portable request across every provider |
| 015 | Provider failure taxonomy | typed hierarchy with a `retryable` flag, classified once in the HTTP base | let httpx errors escape; one error class with a status | Only one provider exists and every failure maps to the same status |
| 016 | Model → provider routing | ordered `pattern=provider` glob table, first match wins | exact-name dict; regexes; provider inferred in code | Selection needs weights, fallbacks or health, not just a name |
| 017 | Where configuration lives | environment variables, parsed at boot | JSON/YAML file; embedded DB (LiteDB/SQLite) | The table must change at runtime — then Redis, not a local file |
| 018 | Streamed usage accounting | always ask the provider for usage; forward the chunk only if the caller asked | collect only when asked; estimate from relayed deltas | Usage reporting becomes billable, or arrives per chunk |
| 019 | Client disconnect on a stream | let the cancellation reach the provider's generator, record the abandonment, re-raise | rely on GC; poll `receive()` for `http.disconnect` | The server never delivers a disconnect and `send()` must be relied on |
| 020 | Provider fallback | ordered `primary>next` chains in config, hopped only on breaker-open or exhausted retries | no fallback; automatic failover on any error | Callers need one portable request across providers with different model names |
| 021 | Per-key rate limits | RPM + TPM as one atomic Lua token bucket in Redis, fail-open | per-process counters; fixed-window `INCR` | Limits become a contractual quota — then fail closed, loudly |
| 022 | Usage ledger | store token counts, price at read time | store dollars (float, or integer nano-USD) | Prices must be frozen per request for audit — then store both |
| 023 | Agent tooling in the repo | committed hooks/skills that shell out to CI's own tools | nothing in-repo; commit personal prefs too | Hooks become personal preference rather than repo policy |
| 024 | Semantic tier placement & matching | exact tier first, semantic only on an exact miss; embed `messages`, hash the rest into the namespace; hits promoted to the exact tier; numpy in-process | semantic first; one vector-keyed tier; embed the whole request; a vector database | Paraphrase traffic dominates and the exact tier's hit rate is noise — then semantic first; the fleet needs one shared index — then rows in Redis, same search |
| 025 | Embedder placement & configuration | own module outside `providers/`, one `EmbeddingError`, switched on by `VORTEX_EMBEDDING_MODEL` with the URL inherited from the Ollama adapter | a fourth chat adapter on `HttpChatAdapter`; widen `ChatProvider` with `embed()`; the full `ProviderError` taxonomy; a `provider:model` scheme | Chat and embedding calls need one shared breaker or budget — then behind the resilience wrapper; two backends serve the same model name — then a prefix |
| 026 | Tracing shape & wiring | hand-written spans in a raw-ASGI middleware, child spans per cache tier, provider call and retry attempt; trace IDs bound into the logs | `opentelemetry-instrumentation-fastapi`; seam spans with no root span; the SDK as an optional extra | OpenTelemetry ships an ASGI instrumentation that is not `BaseHTTPMiddleware` shaped |
| 027 | Metric label cardinality | cap caller-controlled labels at N distinct values and name the overflow `other`; route labelled from the matched template | label with the model as sent; drop the label; allow only models in the routing table | Labels must be per-tenant, which multiplies rather than adds — then the cap moves to the scrape, not the process |
| 028 | `/metrics` placement & auth | open, on the app beside the health probes, behind one switch and a configurable path | under `/v1` behind an API key; a second internal-only port; OTLP metrics to the collector instead | A metric grows a `key_id` label — then it is no longer boring and it belongs behind the key |
| 029 | Disabled instrumentation's cost | `span()` returns one shared inert context manager when no provider is installed | accept the 17%; guard each call site on the setting; a zero-ratio sampler | OpenTelemetry's no-op tracer becomes free, or something depends on a disabled span being in the context |
| 030 | Half-open trial ownership | the trial is one call; endings that learned nothing release in a `finally`; `allow()` returns a `Permit` only its holder can release | the trial keeps its retries; release between attempts; count every trial ending as a failure; `bool` + `try/finally`; an `async with` admission | A provider's first call after idle reliably fails (scale-to-zero, cold starts) — then the trial keeps its retries |
| 031 | Request larger than the token allowance | a `400` with code `request_exceeds_token_limit`, decided from the key before Redis; `vortex-keys create` warns about a key that would refuse most requests | the `429` minus its `Retry-After`; cap the hold at capacity; let the bucket go into debt | Callers routinely send `max_tokens` far above what they use — then debt, in both scripts |
| 032 | When a stream commits to its `200` | at the first chunk of any kind, with the same retries, breaker and hops as a completion until then | commit when the route returns; commit at the first chunk with content; statuses without retries | A vendor's first chunk stops meaning acceptance — then wait for the first chunk with content |
| 033 | How long a Redis call may take | 0.25 s per call and 0.5 s to connect, set on the client from `VORTEX_REDIS_*_TIMEOUT_SECONDS` | no timeout; `wait_for` at each call site; a breaker in front of Redis | A degraded Redis's ~1.25 s (stalled) to ~2.5 s (black-holed) per request breaks the latency SLO — then a breaker that skips Redis while it is sick |
| 034 | How large a request body may be | 10 MiB by default, a `413` from an innermost ASGI middleware that checks `Content-Length` and counts chunked bodies, before auth | `Content-Length` alone; leave it to a reverse proxy; check in the route | The gateway only ever runs behind a proxy that enforces the same cap — then that proxy alone |
| 035 | Starting in development mode outside development | refused in `staging` and `prod`, by `create_app` before it builds anything, with no override flag; so is falling back to the mock provider there | `prod` only; a `Settings` validator; a warning at startup | A staging tier that is genuinely private and keyless by design — then guard `prod` alone |

Fill each row as the ADR lands. The `1xx` range belongs to the RAG repo.
