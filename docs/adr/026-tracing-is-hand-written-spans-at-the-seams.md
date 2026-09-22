# ADR-026: Spans are written by hand at the seams, not installed by auto-instrumentation
Date / Status: 2026-09-22 / accepted
Context: ADR-008 chose structured logs with request IDs and named its own
reversal condition — "need distributed traces/spans, not just correlation IDs".
That day arrived: a retry storm is three `provider call retried` lines that
nothing joins to the request they belong to, and "which part of this request
was slow" is a question the access log's single `duration_ms` cannot answer at
all. The gateway now has four things worth timing separately — each cache tier,
the provider call, and every retry attempt — and they nest.
Options: A) `opentelemetry-instrumentation-fastapi`, which is one line of setup
and installs itself as a `BaseHTTPMiddleware`-shaped wrapper B) hand-written
spans in a raw-ASGI middleware, matching `RequestIDMiddleware`, with explicit
child spans at the four seams C) spans only at the seams and none at the HTTP
edge. Separately: whether the OTel SDK is a dependency or an extra, and whether
the trace ID reaches the logs.
Decision: B, as ordinary dependencies, with the IDs bound into the log context.
`middleware.py` already refuses `BaseHTTPMiddleware` because it buffers through
a second task and does not survive a streaming response, and ADR-018/019 make
streaming the most carefully built thing here — adopting A would put that shape
back under the app to get spans for free. The ASGI layer also turns out to be
the only place that can measure time to first *token*: TTFT is the gap to the
first `http.response.body` message carrying bytes, and nothing above or below
that boundary is handed the message. C was rejected because child spans with no
root are orphans. The SDK is a plain dependency rather than an extra because CI
installs no extras, and an extra is a module mypy never checks and pytest never
imports. The telemetry middleware is mounted *inside* `RequestIDMiddleware`,
which clears structlog's context vars on entry and would otherwise wipe the
trace IDs bound outside it; the cost is that the span misses the outer
middleware's few microseconds and the access log's `duration_ms` always reads a
shade higher than the span.
Consequences: Tracing is off by default and off costs nothing rather than
little: with no SDK provider installed the OTel API hands back a no-op tracer,
so the `span()` calls at the seams stay in the code path unconditionally. Every
log line a request emits now carries `trace_id` and `span_id`, so a line copied
out of the logs pastes into Jaeger. `configure_tracing` returns *ownership*, not
liveness, because OpenTelemetry's provider is a process global that refuses to
be replaced and a second app in one worker must not flush the first's spans on
shutdown. The OTLP/HTTP exporter drags `requests` into a service that already
speaks httpx — the price of a wire format every collector understands. Revisit
if OpenTelemetry ships an ASGI instrumentation that is not `BaseHTTPMiddleware`
shaped, or if the span set stops matching the seams and becomes a second thing
to keep in step with the code.
