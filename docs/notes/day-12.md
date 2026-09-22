## Day 12 — the gateway learns to explain itself, and the reader lies about it
Designed: [day-12 design note](../design/day-12.md)

Built:
- `metrics.py` — one registry of its own, fourteen instruments, and
  `LabelBudget`: `model` comes out of a request body, and a Prometheus label
  value allocates a series that is never freed, so the gateway admits fifty
  distinct values and calls the rest `other` (ADR-027).
- `tracing.py` — the only module that touches the OTel SDK. Spans are written
  by hand because `opentelemetry-instrumentation-fastapi` installs itself in
  the `BaseHTTPMiddleware` shape `middleware.py` already refuses (ADR-026).
  `configure_tracing` returns *ownership*, not liveness.
- `TelemetryMiddleware` — server span, request counters, and time to first
  token, which is the gap to the first `http.response.body` message carrying
  bytes and is visible at no other layer. Mounted *inside* `RequestIDMiddleware`
  because that one clears structlog's context vars on entry, which is what puts
  `trace_id` on every line a request emits.
- Child spans and counters at the seams: each cache tier and the provider call
  in `routes.py`, one span per retry *attempt* in `resilience_wrapper.py`,
  tokens and dollars in `metering.py`, the three stream endings in
  `streaming.py`, the breaker's state as an ordered gauge in `resilience.py`.
- `/metrics`, open, beside the health probes: a scraper has no API key and
  issuing it one puts a credential that can read your traffic volumes into a
  monitoring system's config file (ADR-028).
- `compose.yaml`, `Dockerfile`, `deploy/` — gateway, Redis, Prometheus, Grafana,
  an eighteen-panel dashboard committed as JSON, Jaeger behind a profile — and
  `scripts/generate_traffic.py`, which sends a mixed load and reads the result
  back out of `/metrics`.

**Broke: predicted that a metric line could be matched by its leading text;
observed a reader that reported `0 requests on the chat route` against a gateway
that had just served six hundred of them.** The scrape parser matched
`vortex_requests_total{route="/v1/chat/completions"` as a string prefix.
`prometheus_client` writes labels in the order the *metric* declared them, which
sorts alphabetically, so the real line begins `{method="POST",model=...` and the
prefix never matched anything. Same for `{tier="exact",outcome="HIT"`, which is
written `{outcome="HIT",tier="exact"}`.

```
  gateway published
    requests  601 total, 0 on the chat route     ← 600 of them were on it
```

What makes this worth writing down is not the bug, it is the *direction it
points*. A reader that reports zero for a series that is right there reads
exactly like a gateway that is not publishing the series — so the instinct is to
go and check the instrumentation, which is fine, and correct, and the wrong end
of the problem. Thirty seconds of `curl /metrics | grep` said the series was
there and perfect. The fix was to parse the label set into a mapping and match
on a subset; the lesson is that **a monitoring reader needs to be able to tell
"absent" from "I could not find it"**, and a prefix match cannot.

Broke, minor, and pointed the same way: the first scrape published thirty-odd
series where fourteen instruments were declared. `prometheus_client` emits a
`_created` gauge alongside every counter and histogram — an OpenMetrics feature
that Prometheus's own text scrape ignores. Doubling the series count on the
same day as writing an ADR about bounding the series count is the kind of thing
that only shows up if you read the output rather than the code.
`disable_created_metrics()`, one line.

Near miss, caught by reasoning rather than by a test: `configure_tracing` first
returned "is tracing live", and the lifespan called `shutdown_tracing()` on that.
OTel's tracer provider is a process global that refuses to be replaced, so the
*second* app built in a worker — which is what every test file does, and what a
`TestClient` does on teardown — would have flushed and stopped the provider the
first one was still writing to. Returning ownership instead is the same rule
`create_app` already uses for Redis, the provider and the embedder: whoever
built it closes it. The three lines were already written before I noticed.

The trace that makes the day, against an upstream pointed at a discard port:

```
POST /v1/chat/completions        ERROR  status=502
├─ cache.exact.lookup                   outcome=off
├─ cache.semantic.lookup                outcome=off
└─ provider.complete             ERROR  provider=router
   ├─ provider.attempt           ERROR  attempt=1 of 3
   ├─ provider.attempt           ERROR  attempt=2 of 3
   └─ provider.attempt           ERROR  attempt=3 of 3
```

and the counters after four such requests, with the breaker threshold at two:
six upstream attempts for the first two requests, **zero** for the next two,
`vortex_breaker_state{provider="openai"} 2.0`. Day 5 argued that a breaker turns
a retry storm into a refusal that costs nothing. This is the first time that
claim has been a number instead of a paragraph.

Not done, and said plainly rather than fudged: **there is no dashboard
screenshot.** Docker is not installed on this machine and is not permitted on
it, so `compose.yaml`, the `Dockerfile` and the Grafana provisioning are
unverified configuration — written carefully, never once run. The gateway
numbers in the README are real and were measured natively against Redis and
uvicorn; the stack around them is a promise. The README says which is which,
because a screenshot of a dashboard nobody has stood up would be the most
convincing untrue thing in this repository.

Learned: instrumentation has a second correctness problem that ordinary code
does not. Normal code is wrong loudly — it raises, or returns the wrong answer
to something that checks. A monitoring path is wrong *quietly and plausibly*:
the reader that finds nothing, the label that was silently collapsed, the
`_created` series nobody asked for, the counter that resets and reads as a
restart. Every one of those looks like a true statement about the system. So
the thing to test is not "does the counter go up" — it is the cross product of
the counter with the state the system was in, which is why `test_metrics.py`
covers all three stream endings and a 404 that still names its model, and why
the traffic generator reads its own claims back out of `/metrics` rather than
trusting the tally it kept on the client side.

**One paragraph I could say in an interview.** I instrumented an LLM gateway
with OpenTelemetry and Prometheus, by hand rather than with the FastAPI
auto-instrumentation, because that installs itself in a middleware shape that
breaks streaming — and streaming, with three distinct endings including client
abandonment, is the most carefully built part of the service. Writing the ASGI
middleware myself turned out to be the only way to measure time to first token
at all, since that is the first response body message with bytes in it and no
other layer is handed it. The design decision I would defend hardest is the
cardinality cap: `model` is the most useful label on every metric and it is a
free-text field in the request body, so the gateway admits fifty distinct values
per process and calls the rest `other` — an unbounded label is a memory leak
whose rate is chosen by whoever is calling, and a monitoring system that falls
over is worse than one that gives you a top-fifty breakdown. The bug I learned
most from was in the *reader*, not the gateway: it matched metric lines by a
label prefix, Prometheus writes labels in declaration order, and it reported
zero for a series that was sitting right there — which is indistinguishable from
a gateway that never published it.
