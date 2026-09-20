## Day 09 — answering a question the gateway has already answered
Designed: [day-09 design note](../design/day-09.md)

Built:
- `cache.py` — canonical request hashing (`model_dump(mode="json")`, sorted
  keys, compact separators, SHA-256), an exact Redis entry per request, a
  per-route TTL table parsed from `VORTEX_CACHE_TTLS`, and `X-Cache:
  HIT|MISS|BYPASS`. Four fields are dropped before hashing — `stream`,
  `stream_options`, `user`, `metadata` — and nothing else, because nothing else
  is provably unable to change a generated token (ADR-004).
- Namespacing by `key_id`, with `VORTEX_CACHE_SCOPE=global` for a single-tenant
  deployment. An exact hit can only ever return an answer to a prompt the caller
  sent themselves, so nothing leaks *in*; what it returns is still a completion
  generated under another tenant's key.
- `X-Vortex-Cache-Bypass`, which skips the lookup **and** the store. A dedicated
  header rather than `Cache-Control: no-cache`, which browsers, proxies and
  reloads set for their own reasons — every one of them a bill this gateway did
  not have to pay.
- Per-process hit/miss/bypass/store/error counters on `app.state.cache.stats`,
  for Day 10 to consume.
- `middleware.py` — the access line carries `cache=HIT|MISS|BYPASS`, read off the
  outgoing header at `http.response.start`. Added after the checkpoint review
  below, and not in the day's plan.

**Broke: predicted the rate-limit header on a cache hit would show the refund;
it showed the bucket before it.** The test asserting that a hit gives its token
reservation back read `x-ratelimit-remaining-tokens` off the hit's own response.
It passed. It also passed with the settlement deleted, which is the only fact
that matters:

| implementation | `x-ratelimit-remaining-tokens` on the hit | bucket in Redis after |
|---|---|---|
| hit settles at zero | 9482 | 9995 |
| hit never settles | 9482 | 9482 |

The header is built from the `Decision` the limiter returned at *admission* — it
is a receipt for the reservation, issued before the request was served, and the
settlement it is being asked about happens strictly after it is written. There
is no ordering in which that assertion could have worked. Learned: assert on the
state, not on the receipt. Reading the bucket back out of Redis after the
response distinguishes the two implementations by 513 tokens, and the clock is
pinned while doing it, because at `tpm=10_000` the bucket refills ~167 tokens a
second and a slow runner would refill the difference.

**Broke again: turning the cache on turned metering on.** `create_app` built the
limiter and the ledger from `connection is not None`, which was true exactly
when metering was enabled — until today, when the cache became a second reason
to open a Redis connection. So `VORTEX_CACHE_ENABLED=true` with metering off
produced a gateway that enforced per-key limits and wrote a ledger nobody asked
for, and the config comment claiming the two switches were independent was
written by me, an hour earlier, in the same session.

Nothing failed. Every existing test that passes a `redis=` also sets
`metering_enabled=True`, so the two conditions had never been distinguishable,
and the new tests all read the cache rather than the meter. It surfaced only
from asking what `build(redis, provider)` in the new test file was actually
constructing. Learned: "is the dependency reachable?" and "is this feature on?"
are different questions, and a subsystem gated on the first will change
behaviour the day something else needs the same dependency. The meter is now
gated on `settings.metering_enabled`; the connection is shared, the switches are
not.

**Broke, on the checkpoint review: predicted the counters had made the hit rate
observable; observed that nothing outside the test suite had ever read them.**

`CacheStats` is correct and it is covered. `snapshot()` returns `hit_rate: 0.5`
for one hit and one miss, asserted in `test_cache.py` and again in
`test_integration.py` under a comment naming what it is for — *"the metrics
hooks Day 10 reads"*. Both pass. Neither proves the number can be read by
anyone. The checkpoint asked for the hit rate to be visible in the logs, and
here is a MISS followed immediately by a HIT:

```
{"http_path": "/v1/chat/completions", "http_status": 200, "duration_ms": 2.65, "event": "request completed", ...}
{"http_path": "/v1/chat/completions", "http_status": 200, "duration_ms": 1.07, "event": "request completed", ...}
```

Identical field for field. `duration_ms` differs, and that is a latency
distribution, not a hit rate. `cache.py` logs only its two *failure* paths —
`response cache unavailable` and the stale-entry discard — so a healthy cache is
completely silent, and the rate was not merely unexported but not recoverable by
counting either.

| where you would look | before | after |
|---|---|---|
| `X-Cache` response header | one request, and only the caller sees it | unchanged |
| `CacheStats.snapshot()` | correct, per worker, **no reader in `src/`** | unchanged — still Day 10 |
| the access log | nothing | `cache=HIT\|MISS\|BYPASS` on every line |

Learned: **a counter is not observability until something outside the process
reads it, and a test cannot tell you the difference, because the test is inside
the process.** `assert stats.snapshot() == {...}` reads exactly like an
observability test and is nothing of the kind — it reaches for an in-process
object no operator has, and it would go on passing on a fleet where the number
was unreachable. This is the same shape as the first Broke above, one level up:
*assert on the state, not on the receipt* becomes *assert on the artefact the
reader actually sees*. So the four new tests in `test_middleware.py` parse the
emitted JSON and compute the hit rate from the log lines, rather than asking the
object that already knows the answer.

The second-order one, from writing those tests: `BYPASS` has to stay out of the
denominator. Three lookups and one stream is 2/3, not 2/4 — otherwise a
deployment that streams heavily watches its hit rate fall while the cache does
exactly what ADR-004 says it should. `CacheStats.lookups` already excluded
bypasses; the log query has to as well, which is why the field carries three
values rather than a boolean, and why it is *absent* rather than `null` when no
cache is configured: a run of nulls cannot land in either total.

**The checkpoint's other half, measured.** 300 HITs through the ASGI stack
against a real Redis on loopback, `MockProvider` behind it:

```
min 0.277 ms   p50 0.333 ms   p95 0.392 ms   p99 0.496 ms   max 0.588 ms
provider calls: 1
```

Under 5 ms was the bar and the p99 is about a tenth of it. Two caveats on what
that number covers: it is `httpx.ASGITransport` in-process, so it excludes
uvicorn's socket handling, and Redis is on loopback rather than across a network
hop. Neither is plausibly worth the remaining 4.5 ms, but it is
gateway-plus-Redis overhead, not a wire measurement.

**Experiment, as predicted.** Two identical requests through a metered,
cache-enabled gateway: `provider.call_count == 1`, the second response
byte-identical to the first including its `id`, `X-Cache: HIT`, and the ledger
reporting `total_requests == 2` with `total_tokens` equal to the first
response's usage alone. The number worth watching was the ledger, and it is the
one a header-only test would have got wrong: recording the cached response's
usage would have doubled the reported spend against an invoice that only ever
saw one call.

The mutation checks are the part I would repeat. Deleting the hit's settlement
failed two tests; ignoring the bypass header failed one; letting streams into
the cache failed three, including the one asserting a stored completion is never
handed to a client parsing SSE. A cache is unusually easy to test wrongly,
because the happy path — same body twice, second one faster — passes against an
implementation that also serves a stream a JSON blob and bills the caller twice
for it.

**Broke, from the tooling: `/verify` failed three tests that had just passed,
and the code was fine.** `uv sync --locked --no-editable` installs a built wheel
rather than linking `src/`, which is the whole point of the step — it is what
proves the suite runs against the packaged distribution. What it does *not* do
is rebuild that wheel when the local package's version string has not changed.
The second run in the same tree reports:

```
Resolved 50 packages in 0.61ms
Checked 49 packages in 0.30ms
```

— reinstalls nothing, and the suite then tests a wheel that predates every edit
since the first `/verify`. The three failing tests were reading a
`middleware.py` in `site-packages` that had no `cache` field in it, because the
one I had written was still only in `src/`.

| run | what the suite imported | result |
|---|---|---|
| `pytest` after the edit (venv still editable) | `src/` via the `.pth` | 594 passed |
| `/verify`, first `--no-editable` sync | cached wheel, built pre-edit | 3 failed |
| `/verify` with `--reinstall-package` | wheel built from current source | 594 passed |

Learned: the symptom is the *inverse* of the one the src/ layout was built to
produce, which is why it is disorienting. The layout exists so a broken wheel
fails a passing source tree; here a fixed source tree failed because the wheel
was stale, and the suite was right both times — it reports the truth about the
wheel, and the wheel was old. CI never sees this, because a fresh runner builds
once into an empty venv, so it is a purely local trap and it costs exactly as
long as it takes to stop trusting your own diff. `/verify` now passes
`--reinstall-package vortex-ai-gateway`. The narrower lesson: a version string
is a cache key, and hand-editing one to a value that has been built before
(`0.1.0` → `0.0.1a1`, in the uncommitted `pyproject.toml` diff this tree is
carrying) is enough to hand you a wheel from a previous session.

**Quiz misses:** the difference between `Cache-Control: no-cache` and
`no-store`; what a `Vary` header would mean for a gateway keyed on a request
body rather than a URL.

**One paragraph I could say in an interview.** Caching an LLM gateway looks like
memoisation and isn't, because three of the decisions are about money and
tenancy rather than about hashing. The key is a SHA-256 over the *validated*
request dumped with sorted keys, not over the bytes the client sent, so a
library changing its JSON field order doesn't silently halve the hit rate — and
only the four fields that cannot change a generated token are excluded, so a
caller who moves `seed` or `n` is asking a different question and gets one.
Entries are namespaced per API key by default, because an exact hit hands one
tenant a completion that was generated under, and billed to, another tenant's
account; a global namespace is a config flag for deployments where every caller
is the same customer. A hit still goes through the rate limiter — otherwise the
cache is a way around it — but it settles at zero tokens rather than at the
cached response's usage, because the ledger exists to be reconciled against the
vendor's invoice and those tokens were never bought. And streaming requests
bypass it in both directions for now: two of a stream's three endings produce a
partial answer that must never be stored, so caching streams means teaching the
cache the ending taxonomy the streaming module owns, which is a bigger feature
than it looks and is the one I'd build next if the traffic justified it.
