## Day 13 — finding out what the gateway costs

**Building.** Every performance claim in this repository is currently either a
single number from one ad-hoc run (`477/s`, from `generate_traffic.py`) or a
paragraph. That is enough to say the thing works and not enough to say anything
about it: there is no p99, no error rate under saturation, no statement of what
the gateway's *own* overhead is as distinct from an upstream's latency, and no
evidence either way about whether four uvicorn workers are four times one. So:
a committed k6 suite of four workloads, a runner that sweeps the two
configuration axes that matter, a README table with the methodology next to it,
and one bottleneck fixed with the measurement that found it.

The four workloads, and what each is *for* — a load script that does not name
the question it answers is a number generator:

1. **Baseline**, against the mock provider with every prompt unique. The mock
   answers in constant time with no network, so what this measures is the
   gateway: ASGI, two middlewares, auth, admission, a cache lookup that misses,
   validation, serialisation. This is the only number in the suite that is
   about *our* code, and it is the denominator for every other claim.
2. **Cache hit**, a small prompt pool warmed first. The exact tier's whole
   promise is a hash and a Redis `GET` in place of a provider call, and the
   number that promise lives or dies on is the p99 of a HIT, not the mean.
3. **Streaming**, SSE with the body read to completion. Buffered and streamed
   responses leave the gateway through different code — `StreamingResponse`,
   the per-chunk accounting in `streaming.py`, the settlement in a `finally` —
   and none of it is on the buffered path.
4. **Provider failure**, with one provider routed at a dead upstream. This is
   the one that tests a claim Day 5 made and Day 12 only partly demonstrated:
   that a breaker turns a retry storm into a refusal that costs nothing. Under
   *load* the interesting question is not whether the breaker opens, it is
   whether the failing path steals the event loop from requests that never
   needed that provider.

**Alternatives — the load generator.** (1) Extend
`scripts/generate_traffic.py`, which already sends a mixed load and already
parses `/metrics`. It is in-repo, needs no install, and is written in the
language everything else here is written in. But it is a closed-loop
`asyncio.gather` over a semaphore, its "p95" is a sorted list indexed by hand,
and — the disqualifying part — it shares a process, an event loop and a GIL
with nothing, but it does share a *machine* with the thing under test, and an
`httpx` client saturating 11 cores from Python will hit its own ceiling before
the gateway's. A load generator whose own overhead is the limit measures the
load generator. (2) `locust`, which is Python and would go in
`pyproject.toml` — same event-loop ceiling, plus a dev dependency that CI then
resolves for a tool CI never runs. (3) `k6`: the generator is Go, the test
script is JS, it has percentiles, thresholds, open- and closed-loop scenario
executors, and a machine-readable summary. Chose (3), with `generate_traffic.py`
left exactly as it is — it has a different job (put something on every panel of
a dashboard) and is better at it than a load tool would be.

**Alternatives — open loop or closed loop.** (1) Closed loop (`constant-vus`):
N virtual users each sending the next request when the last one returns.
Latency is then reported at a known concurrency, which is the honest way to
compare two builds, and throughput is whatever falls out. (2) Open loop
(`constant-arrival-rate`): a fixed request rate regardless of whether the
gateway is keeping up, which is the only way to see an error rate under
saturation and the only shape that resembles real traffic. Chose **both**, as
one `BENCH_MODE` switch in a shared module, because they answer different
questions and picking one silently answers the other one wrongly: a closed-loop
run cannot produce a queue, and an open-loop run's latency number is
meaningless unless you also say whether it kept up.

**Alternatives — where TTFT comes from.** k6 cannot read an SSE body
incrementally without the `xk6-sse` extension, which needs a custom binary and
so cannot be a committed script anybody can run. (1) Require the extension.
(2) Drop TTFT from the client side and read the gateway's own
`vortex_stream_ttft_seconds` histogram. (3) Use k6's `timings.waiting`, which
is time-to-first-byte, and for a `text/event-stream` response the first byte is
the first flushed chunk. Chose (3) **and** (2): `waiting` is the client's view,
the histogram is the gateway's, and the pair is a cross-check of exactly the
kind Day 12 said instrumentation needs — two independent measurements of one
quantity, where a disagreement is a bug in the instrument.

**Alternatives — the configuration sweep.** The axes worth sweeping are the
ones a deployment actually chooses. (1) Sweep everything: workers, cache,
semantic tier, metering, tracing, log level — 64 arms, six hours, and nobody
reads the table. (2) Sweep the two that are load-bearing and hold the rest
fixed: **1 worker vs 4** (does this thing scale across processes, given that
breaker state, cache counters, Prometheus counters and the semantic index are
all per process) and **semantic tier off vs on** (the tier costs an embedding
call on every exact-tier miss, and ADR-005 keeps it off; what it costs is the
other half of that decision). Chose (2), four arms per workload.

**Planned experiment.** Prediction, written before the run: the baseline
p50 sits in the low single-digit milliseconds and the gateway's overhead is
dominated by two things — JSON serialisation and the per-request log write —
because there is nothing else on that path that is not an attribute lookup.
Specifically: `routes.py` serialises every response *twice*, once with
`model_dump(mode="json")` into a Python dict and again with `json.dumps` inside
`JSONResponse`, and the cache-hit path does it three times counting the
`model_validate_json` on the way out of Redis. So the cache-hit workload should
be *slower* per request than it looks like it ought to be, and the gap between
the cache-hit p50 and the baseline p50 should be much smaller than the Redis
round trip alone would suggest — because the hit is paying for a validation and
a re-serialisation the miss does not. Four workers should be close to linear on
the baseline, since there is no shared state on that path, and *less* than
linear on the cache-hit workload, where four processes contend for one Redis
connection each against one single-threaded server. If four workers are not
faster than one on the baseline, the bottleneck is the accept queue or stdout,
both of which are shared.
