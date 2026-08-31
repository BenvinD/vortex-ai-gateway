# ADR-008: Structured JSON logging with request-ID propagation
Date / Status: 2026-08-31 / accepted
Context: A gateway multiplexes many upstreams; debugging needs every log line
tied to one client request and machine-parseable by the platform collector.
Options: A) stdlib logging with a text format B) structlog rendering JSON to
stdout, request ID bound via contextvars in raw-ASGI middleware C) OpenTelemetry
tracing now.
Decision: B. `configure_logging(settings)` sets a structlog JSON pipeline at
`settings.log_level`. `RequestIDMiddleware` (raw ASGI, so streaming stays
correct) takes the inbound `X-Request-ID` or mints a UUID, binds it to
contextvars so every line carries `request_id`, exposes it on
`request.state.request_id`, echoes it back, and logs one `request completed`
line per request.
Consequences: Logs are JSON even in local dev (pipe through `jq`). Library logs
still go through stdlib until a `ProcessorFormatter` bridge is added. OTel (C)
can layer on later and reuse the same request ID as trace context.
