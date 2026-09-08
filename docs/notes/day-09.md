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
