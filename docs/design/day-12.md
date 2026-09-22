## Day 12 — making the gateway explain itself

**Building.** Everything this gateway does that is interesting is currently
invisible unless somebody greps the logs: a retry storm is three log lines
nobody correlated, a cache hit rate is a `jq` pipeline, and "which model costs
us money" is a Redis scan. Three things, in one day. One OTel trace per request
with child spans for each cache tier, the provider call, and every retry
attempt — so the storm is a shape on a screen rather than an inference. A
Prometheus `/metrics` endpoint with request counters, latency and TTFT
histograms, cache outcomes per tier, retry/fallback/breaker counters, and
tokens and dollars. Then a compose stack — gateway, Redis, Prometheus,
Grafana — with a provisioned dashboard committed to the repo, and a traffic
generator to put something on it.

**Alternatives — how spans get created.** (1) `opentelemetry-instrumentation-fastapi`:
one line of setup, spans for free, and it installs itself as a
`BaseHTTPMiddleware`-shaped wrapper. This codebase already refuses that shape
once, in `middleware.py`, because it breaks streaming and adds a task hop — and
streaming with a correct ending taxonomy is ADR-018/019, the most expensive
thing here. (2) Manual spans in a raw-ASGI middleware, matching
`RequestIDMiddleware`. (3) Spans only at the interesting seams — cache,
provider, retry — and none at the HTTP edge, so there is no root to hang them
from. Chose (2). It also turns out to be the only layer that can measure time
to first *token*: TTFT is the gap between the request arriving and the first
non-empty `http.response.body` message, and nothing above or below the ASGI
boundary sees that message.

**Alternatives — where the trace ID meets the logs.** (1) Leave them
unconnected, and correlate by timestamp. (2) Put the request ID on the span as
an attribute. (3) Both: the span carries `vortex.request_id`, and the trace and
span IDs are bound into structlog's context vars so every line the request
emits carries them. Chose (3), which fixes the middleware order: the telemetry
middleware must run *inside* `RequestIDMiddleware`, because that one clears the
context vars on entry and would wipe anything bound outside it. The span
therefore misses a few microseconds of the outer middleware, which is a price
worth paying for `trace_id` on the "request completed" line.

**Alternatives — metric labels a caller controls.** `model` is the most useful
label on every metric here and it is a free-text field in the request body.
(1) Label with it and accept that a client looping over random model names
allocates a time series per name, forever, in a process that never restarts.
(2) Drop the label, and lose the one breakdown anybody actually wants.
(3) Label with it up to a fixed number of distinct values per metric and call
everything after that `other`. Chose (3) — `metrics.LabelBudget`, ~20 lines,
and the cap is a setting. A dashboard that says "your top 50 models, plus
`other`" is the honest version of the dashboard anyway, and the failure mode of
(1) is an outage caused by the tool that was supposed to detect outages.

**Alternatives — who counts a cache hit.** `CacheStats` already counts them
per process, and both tiers have one. (1) Mirror those counters into Prometheus
inside `cache.py` and `semantic.py`, which puts a metrics import into two
modules that currently know nothing about being observed. (2) Read the outcome
off the response headers in the middleware, which is exactly where the access
log already gets it, and which describes a response served by a tier the
middleware has never heard of. Chose (2). One place decides what a cache
outcome is, and it is the same place for logs and metrics.

**Alternatives — what ships in the image.** (1) Run Prometheus and Grafana as
part of the gateway process. (2) Ship the compose stack and the dashboard JSON
as configuration in the repo, with the gateway exposing only `/metrics` and
OTLP. Chose (2), obviously, but it is worth saying why the *dashboard* is in
the repo and not in Grafana's own storage: a dashboard nobody can review is a
dashboard that drifts, and a provisioned JSON file shows up in a diff when
somebody changes what "healthy" looks like.

**Planned experiment.** Run the traffic generator against a gateway whose mock
provider fails a fixed share of calls, with retries on and the breaker
threshold low. Prediction: the retry counter rises roughly in step with the
failure count and the trace shows two or three attempt spans under one provider
span. The number to watch is `vortex_provider_attempts_total` against
`vortex_requests_total` — if attempts climb faster than linearly with the
failure rate, the retry budget is not doing what Day 5 said it would, and the
breaker should have opened before it got there.
