# Architecture Decision Records

One ADR per decision, 6–10 lines. Written *after* the decision, *before* moving on.
Copy `000-template.md` to `NNN-short-title.md` and fill it in.

These are the interview answers. If a decision has no ADR, it will not survive
being questioned six weeks from now.

## Index

This repo is the gateway (Gatewright), which owns the `0xx` range.

| # | Decision | Chose | Rejected | When the rejected option wins |
|---|----------|-------|----------|-------------------------------|
| 001 | Retry policy | | | |
| 002 | Breaker thresholds | | | |
| 003 | Key storage | | | |
| 004 | Streaming cache policy | | | |
| 005 | Semantic-cache threshold/model | | | |
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

Fill each row as the ADR lands. The `1xx` range belongs to the RAG repo.
