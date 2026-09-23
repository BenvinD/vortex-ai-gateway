## Day 13 — measuring the gateway, and being wrong about where the time went
Designed: [day-13 design note](../design/day-13.md)

Built:
- `bench/` — four k6 workloads, each written to answer one question rather than
  to generate a number: `baseline.js` (unique prompts against the mock, so the
  measurement is *our* code and is the denominator for everything else),
  `cache-hit.js` (a warmed pool; the number is the p99 of a hit), `streaming.js`
  (the path `StreamingResponse` and the ADR-018 usage strip live on), and
  `provider-failure.js` (a blackholed upstream driven open-loop, with a
  `GET /v1/usage` bystander whose p99 is the actual assertion).
- `bench/lib/common.js` — one concurrency model for all four, switched between
  closed and open loop by `BENCH_MODE`, because a closed-loop run cannot produce
  a queue and an open-loop run's latency means nothing unless you also say
  whether it kept up. No remote imports: a benchmark that needs the internet to
  start cannot be run on the machine it is measuring.
- `bench/run-matrix.sh` — the 2×2 sweep (1 vs 4 workers, semantic off vs on),
  its own Redis on its own port, flushed between arms, and **all 48 `VORTEX_*`
  settings named explicitly** rather than only the ones an arm varies, because
  `Settings` reads a `.env` as a lower-priority source and an unset knob is one
  a developer's laptop chooses.
- `bench/report.py` — the README table, a 1-vs-4 scaling ratio reported against
  4.00x rather than 1.00x, and a cross-check of the client's numbers against the
  gateway's own. Tested against a fixture of k6's summary shape.
- The fix the exercise found, ADR-029, and the two corrected sentences it made
  necessary in `config.py` and `AGENTS.md`.

**Broke: predicted the gateway's overhead would be dominated by JSON
serialisation and the per-request log write; observed that the largest single
item was OpenTelemetry instrumentation that was switched off.** The design note
committed to the prediction in writing, including the mechanism — `routes.py`
serialises every response twice, once into a dict with `model_dump(mode="json")`
and again into bytes inside `JSONResponse`. That part was true. It is also worth
3–10 µs out of 131, which is 2%.

What actually cost 17% was `span()`:

```
  as-is                        161.8 us/req     6181/s
  no-op spans                  136.1 us/req     7347/s      <- 25.7 us, 15.9%
  no access log                145.2 us/req     6889/s      <- 16.6 us, 10.3%
```

`NoOpTracer.start_as_current_span` is a `@contextmanager`, the `use_span` inside
it is another, and `tracing.span` wrapped both in a third: four spans a request
is twelve generator context managers built, entered and unwound on a gateway
that has never heard of a collector. After short-circuiting `span()` to return
one shared inert context manager, measured across a `git stash` of exactly that
change, twice, 8,000 requests × 9 runs each:

```
  before   min 158.5  p50 170.2  us/req     6307/s
  after    min 130.7  p50 131.2  us/req     7649/s     -17.3%, +21.3%
```

The variance collapsed too — 158–188 before, 131–139 after — which is what
twelve fewer allocations per request looks like from the outside.

The lesson is not "measure before optimising", which everybody already says. It
is narrower and it is about *this* codebase: the false claim was **written down
three times**, in `tracing.py`'s docstring, in a comment in `config.py`, and in
`AGENTS.md`, each time with a plausible mechanism attached ("the OTel API is a
no-op implementation until an SDK provider is installed"). Every one of those
sentences is true. The conclusion drawn from them — therefore it is free — is
not, because a no-op is an object, and an object is an allocation. A confident
comment explaining why something costs nothing is exactly the thing that stops
anyone measuring it, so it now says *why* it is free, which is because
`span()` returns early, which is a fact a test can hold.

**Broke, and it pointed the same direction as Day 12's reader bug: the profiler
lied, and it lied in the shape of a gateway bug.** The first profile showed
`importlib._bootstrap_external.find_spec` called 15,005 times for 3,001 requests
and 15,006 `posix.stat` calls — five filesystem imports per request, which reads
unmistakably as an `import` statement inside a request handler, and is the kind
of finding you go and fix. `print_callers` said the caller was
`httpx/_transports/asgi.py:29(is_running_trio)`. It was the measuring rig, not
the thing measured. The rewrite drives the ASGI callable directly with a
hand-built scope and no HTTP client, which is what made the 25.7 µs visible at
all — it had been buried under the client's own cost. Day 12's version of this
was a metrics reader that reported zero for a series that was present; this one
is a profiler attributing its own overhead to the subject. Both look like true
statements about the system.

Not done, and said plainly rather than fudged: **the matrix has not been run.**
k6 is not installed on this machine, and installing it was declined, so every
cell of the README's benchmark table is an em dash. The scripts parse as ES
modules, `report.py` is tested against a fixture of the summary shape k6 emits,
and `run-matrix.sh` passes `bash -n` — none of which is evidence that a k6 run
would succeed, and the README says so. The measured numbers in this note come
from a different and much narrower instrument: one process, no socket, no second
worker, no error rate. It can find a bottleneck in our own code, which is what
it was built for. It cannot answer the two questions the matrix exists for,
which are whether four workers are four times one and what the semantic tier
costs under load. Still no Grafana screenshot either, for the same reason as
Day 12.

Found while reading, not measured, and deliberately left alone: the semantic
tier's `VectorIndex` appends a row and retains the entire
`ChatCompletionResponse` for every miss, per namespace, with no eviction and no
TTL, while `nearest()` is a matrix-vector product over all live rows. On
caller-supplied traffic both the memory and the lookup cost grow without bound.
That is the same shape as the problem ADR-027 caps for metric labels — a
resource whose growth rate is chosen by whoever is calling — and it is a design
decision with an ADR in it, not a bottleneck fix, in a tier that is off by
default anyway. It is now written down in `bench/README.md` and the root README
so that turning the tier on has to walk past it.

Learned: a benchmark's first job is to have an opinion about what it is
measuring, and the four scripts are more valuable for their headers than for
their bodies. "Baseline is the denominator", "the number is the p99 of a hit",
"the bystander's p99 is the assertion" — each of those is a sentence that makes
one result meaningful and rules out reading it as something else. The second
thing is that the instrument has to be cheaper than the effect: a 25 µs finding
is invisible under an httpx client, and the honest response to "we cannot
measure this" is a smaller rig, not a bigger sample.

**One paragraph I could say in an interview.** I built a k6 benchmark suite for
an LLM gateway where each workload names the question it answers, and swept the
two configuration axes the code could not answer by inspection — worker count,
because the breakers and caches and metric registries are per process, and the
semantic cache tier, because it costs an embedding on every miss. Then I went
looking for the bottleneck the exercise was supposed to find and got it wrong on
purpose-in-writing first: I had predicted JSON serialisation, which the gateway
genuinely does twice per response, and that turned out to be 2% of a request.
The real finding was that the OpenTelemetry instrumentation cost 17% *while
switched off*, because a no-op tracer still builds two generator context
managers per span and my own wrapper made three — twelve per request at four
spans. Fixing it in the one module that owns the seam's API, rather than at six
call sites, cut 158.5 µs to 131.1 and took single-process throughput from 6,307
to 7,649 requests a second. What I would want to be judged on is that the wrong
claim had been written down three times with a plausible mechanism attached, and
that a comment confidently explaining why something is free is the most reliable
way to stop anyone checking.
