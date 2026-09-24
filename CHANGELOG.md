# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Every decision named below has an
ADR in [`docs/adr/`](docs/adr/README.md).

## [0.1.0] — 2026-09-24

The first release: an OpenAI-compatible gateway that routes, limits, caches,
retries and observes calls to OpenAI, Anthropic and Ollama from one process.
Point any OpenAI SDK at it by changing the base URL. With no configuration it
runs end to end on a built-in mock provider, with no Redis and no vendor keys.

### The request path

- **One wire format.** `POST /v1/chat/completions` accepts and returns the
  OpenAI chat-completions shape, buffered or as server-sent events ending in
  `[DONE]`. Unknown fields are rejected with OpenAI's `400` error envelope
  rather than silently dropped (ADR-009, ADR-010, ADR-012).
- **Routing by model name.** `VORTEX_MODEL_ROUTES` is an ordered table of
  shell-glob rules, where the first match wins. The table is also the enable
  list: a routed provider with no key stops the gateway at startup, not on its
  first request (ADR-016, ADR-017).
- **Three vendor adapters,** each translating both ways and refusing a
  parameter the vendor cannot express instead of dropping it. Every failure
  is classified once into a typed taxonomy with a `retryable` flag
  (ADR-011, ADR-014, ADR-015).
- **Resilience per provider.** Full-jitter retries inside a wall-clock
  deadline, an optional aggregate retry budget, one circuit breaker per
  provider with a single half-open trial, and ordered fallback chains
  (`openai>anthropic`) (ADR-001, ADR-002, ADR-020).
- **Streams are always paid for.** The gateway asks every provider for usage
  and strips that chunk back out when the caller did not ask for it. A client
  that hangs up mid-stream cancels the upstream generation and is logged as
  `abandoned` (ADR-018, ADR-019).

### Keys, limits and cost

- **Hashed, revocable client keys** in SQLite, minted by the `vortex-keys`
  CLI. A key is `vtx_<key_id>_<secret>`, and the public `key_id` is what
  limits, bills, caches and logs are keyed on (ADR-003, ADR-013).
- **Per-key RPM and TPM limits** as one atomic token-bucket Lua script in
  Redis, with `X-RateLimit-*` headers on every response. The limiter fails
  open (ADR-021).
- **A usage ledger in tokens, priced at read time,** so correcting a price
  corrects the history. `GET /v1/usage` reports it (ADR-022).

### Caching

- **Exact response cache** in Redis, keyed on a canonical SHA-256 of the
  validated request and namespaced per key. Streams bypass it, and a hit is
  settled at zero tokens. `X-Cache: HIT|MISS|BYPASS` reports the outcome
  (ADR-004).
- **Semantic tier** behind the exact one, with Ollama embeddings and cosine
  similarity. It is shipped **off**: with the two embedders measured, no
  threshold admits paraphrases without also admitting one-token near misses
  (ADR-005, ADR-024, ADR-025).

### Observability

- **Prometheus metrics** on `/metrics` from a registry of their own. The
  caller-controlled `model` label is capped at a label budget (ADR-027,
  ADR-028).
- **OpenTelemetry traces:** one trace per request, with child spans per cache
  tier, per provider call and per retry attempt. When tracing is off, each span
  costs one `is None` test. The first version of that path cost 16% of a
  request (ADR-026, ADR-029).
- **Structured JSON logs** with a request ID, and trace and span IDs, on every
  line (ADR-008).
- **Health probes:** `/healthz` for liveness and `/readyz` for readiness. Redis
  is deliberately not a readiness check (ADR-006).

### Running it

- **A production image:** two stages, `python:3.14-slim-bookworm`, installed
  from a built wheel exactly as CI installs it, run as UID 10001, and
  health-checked with no `curl` in the image.
- **The reference stack** in `compose.yaml`: gateway, Redis, Prometheus,
  Grafana with a provisioned 18-panel dashboard, and Jaeger behind a `tracing`
  profile. Every service has a healthcheck, and startup waits on health.
- **`make demo`** brings the whole stack up healthy with traffic on the
  dashboard, in one command and with no vendor keys.
- **CI** runs lint, format, strict mypy and the test suite against a
  non-editable install. A second job builds the image, asserts it runs
  non-root, brings the stack to healthy, and checks that Prometheus scrapes the
  gateway and that Jaeger receives its traces.
- **Benchmarks:** k6 workloads for baseline, cache hit, streaming and provider
  failure, a matrix runner, and a report. The results are in the README.

### Known limitations

- Circuit-breaker state, cache counters, Prometheus series and the semantic
  index are **per worker process**. The image runs one worker per container;
  scale out with more containers.
- When a provider goes hard down, the breaker only opens after
  `breaker_failure_threshold` requests have spent their retries. Under
  concurrent load the first ~30 s of `503`s are therefore slow, not cheap (see
  README, *How to read it*).
- The published benchmark table was produced with a reconstructed
  `bench/lib/common.js`. The original is now committed, and the table should be
  treated as provisional until the matrix is rerun.
- Not yet built: PII guardrails, a number/unit/entity guard in front of the
  semantic tier, and Redis-backed load balancing.

[0.1.0]: https://github.com/BenvinD/vortex-ai-gateway/releases/tag/v0.1.0
