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

Fill each row as the ADR lands. The `1xx` range belongs to the RAG repo.
