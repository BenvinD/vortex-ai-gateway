# bench/ — what the gateway costs

Four k6 workloads, a runner that sweeps two configuration axes, and a reporter
that turns the result into the table in the root `README.md`. Designed in
`docs/design/day-13.md`; the findings are in `docs/notes/day-13.md`.

```bash
brew install k6                 # or grafana.com/docs/k6/latest/set-up/install-k6/
bench/run-matrix.sh             # the full matrix, ~8 minutes
uv run bench/report.py bench/results/<timestamp>
```

Nothing here is wired into CI. A benchmark that gates a merge on a wall-clock
number measured on a shared runner fails for reasons that have nothing to do
with the change, and the `thresholds` in each script are regression tripwires
for a human running the suite deliberately, not a build gate.

## The four workloads, and the question each one answers

| Script | Answers |
|---|---|
| `baseline.js` | What does *our* code cost? Unique prompts, so the cache always misses; the mock provider answers with no network and no sleep. Everything measured is gateway: two ASGI middlewares, auth, admission, a cache lookup that hashes and misses, validation, serialisation. **This is the denominator for every other number here.** |
| `cache-hit.js` | What does the exact tier save? A small prompt pool, warmed in `setup()` so the measured window is steady-state hits rather than the transient that fills the cache. The number it exists for is the **p99 of a HIT** — a cache whose median is a hash and whose tail is a Redis timeout has moved the variance, not removed a provider call. `BENCH_SEMANTIC=on` sends paraphrases instead, which the exact tier cannot match and the semantic tier is supposed to. |
| `streaming.js` | Is the streamed path the same service? `StreamingResponse`, the per-chunk accounting in `streaming.py`, the usage chunk that is always requested and conditionally stripped (ADR-018), and a settlement in a `finally` — none of it is on the buffered path. Half the iterations ask for usage so both sides of ADR-018 run. |
| `provider-failure.js` | Does a dead vendor cost anything it shouldn't? One provider routed at a blackholed address, driven open-loop, while a second scenario holds a steady trickle of `GET /v1/usage` — authenticated, reads Redis, touches no provider. **The bystander's p99 is the assertion.** If it degrades in step with the storm, one dead vendor is a whole-gateway outage and the breaker is not buying what ADR-002 says it buys. |

## Methodology

**Closed loop by default, open loop on request.** `BENCH_MODE=vus` (the default)
runs N virtual users that each send the next request when the last returns:
latency at a known concurrency, which is the honest way to compare two builds,
and throughput is whatever falls out. `BENCH_MODE=rate` holds a fixed arrival
rate whether or not the gateway keeps up, which is the only shape that can show
an error rate under saturation. Both exist because picking one silently answers
the other wrongly — a closed-loop run cannot produce a queue, and an open-loop
run's latency is meaningless unless you also say whether it kept up.

**The generator is not the thing under test.** k6 is Go; a Python load generator
sharing 11 cores with the gateway hits its own ceiling first and then reports
that ceiling as the gateway's. `scripts/generate_traffic.py` stays as it is — it
has a different job (put something on every panel of the dashboard) and is
better at that than a load tool would be.

**Everything is pinned.** `run-matrix.sh` names all 48 `VORTEX_*` settings when
it starts the gateway, not just the ones an arm varies, because `Settings` reads
a `.env` in the repo root as a lower-priority source and an unset knob is one a
developer's local file silently chooses. Redis is this script's own, on its own
port, flushed between arms. The manifest records the commit, the host, the tool
versions, and whether the tree was dirty.

**The rate limits are enormous rather than zero.** A principal with no limits
never reaches Redis (`RateLimiter.admit` returns early), so zero would quietly
lift the limiter's Lua round trip *out* of the measurement — and the deployment
being benchmarked is one that runs the limiter.

**TTFT is measured twice.** k6 cannot read an SSE body incrementally without the
`xk6-sse` extension, which needs a custom binary and so cannot be a committed
script. What it can report is `timings.waiting` — time to first byte — and for a
`text/event-stream` response the first byte is the first flushed chunk. That
client-side number goes next to the gateway's own
`vortex_stream_ttft_seconds` histogram in `report.py`'s cross-check. Two
independent measurements of one quantity, because Day 12's lesson was that a
monitoring path fails quietly and plausibly, and a disagreement here is a bug in
one of the two instruments rather than a fact about the service.

## Caveats that change how the numbers read

* **Four workers are four caches, four breakers and four indexes.** Breaker
  state, `CacheStats`, the Prometheus registry and the semantic index are all per
  worker process. Only the exact cache (Redis) and the ledger are shared. So a
  `/metrics` scrape reaches whichever worker the OS gave the connection to —
  which is why `report.py` sums a metric over its label sets instead of matching
  one series — and the semantic arm's hit rate is bounded by roughly `1/workers`
  until every process has seen every prompt. `cache-hit.js` warms
  `BENCH_WARM_PASSES` times over for that reason.
* **The semantic index is unbounded.** `VectorIndex` appends a row and holds the
  whole `ChatCompletionResponse` for every miss, per namespace, with no eviction
  and no TTL, and `nearest()` is a full matrix-vector product over live rows. On
  a workload of unique prompts with the tier on, both memory and lookup cost grow
  for the length of the run, so a long semantic arm measures a moving target.
  That is a real property of the tier, not an artefact of the harness — see
  `docs/notes/day-13.md`.
* **The mock provider is not a vendor.** It answers in constant time from a
  derived reply. Every latency here excludes the thing that dominates a real
  request, which is the point: this measures the gateway, and a number that
  included a vendor's p99 would measure the vendor.
* **One machine.** Generator and gateway share 11 cores, so the four-worker arms
  are competing with k6 for CPU. The scaling ratio `report.py` prints is a floor.

## Environment

| Variable | Default | |
|---|---|---|
| `BENCH_WORKERS` | `1 4` | uvicorn worker counts to sweep |
| `BENCH_SEMANTIC_ARMS` | `off on` | semantic tier arms; the `on` arm is skipped with a message if no embedder answers |
| `BENCH_WORKLOADS` | all four | which scripts to run |
| `BENCH_MODE` | `vus` | `vus` (closed loop) or `rate` (open loop) |
| `BENCH_VUS` / `BENCH_RATE` | `32` / `1000` | concurrency, or target arrival rate |
| `BENCH_DURATION` | `30s` | measured window per arm |
| `BENCH_PORT` / `BENCH_REDIS_PORT` | `8100` / `6399` | so a running dev stack is untouched |
| `BENCH_EMBEDDING_MODEL` | `nomic-embed-text` | must already be pulled in Ollama |
| `BENCH_DEAD_UPSTREAM` | `http://10.255.255.1:1` | blackholed, so each attempt costs a connect timeout. `http://127.0.0.1:9` refuses instead, which measures the retry loop's overhead and makes the breaker look unnecessary |
| `BENCH_BREAKER_THRESHOLD` | `5` | lower it to see the breaker open sooner |

A single script can also be run directly, against a gateway you started
yourself:

```bash
BENCH_URL=http://127.0.0.1:8000 k6 run bench/baseline.js
```
