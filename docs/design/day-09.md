## Day 09 — answering a question the gateway has already answered

**Building.** Every request currently reaches a provider, including the one
that is byte-identical to the request served ninety seconds ago. An exact
response cache: a canonical hash of the request, a Redis entry with a per-route
TTL, an `X-Cache: HIT|MISS|BYPASS` header so a caller can see which happened,
a bypass header so an operator can get an answer the cache cannot have touched,
and hit/miss counters for Day 10 to consume.

**Alternatives — what the key is made of.** (1) Hash the raw request body:
one round trip, and `{"model":"x","messages":[…]}` and the same object with the
keys the other way round are different cache entries, so a client library that
reorders its JSON halves the hit rate. (2) Hash the *validated model* dumped
with `sort_keys` and defaults filled in: a request omitting `temperature` and
one sending `1.0` are the same question and hash the same, and field order stops
mattering. (3) Normalise semantically — lowercase, strip whitespace, collapse
near-identical prompts. Chose (2). (3) is Day 10's job and needs an embedding
model and a threshold (ADR-005); doing it by hand here would be a similarity
cache that cannot say how similar. Fields that cannot change the generated
content — `stream`, `stream_options`, `user`, `metadata` — are dropped before
hashing; everything else is in the key, including `seed` and `n`.

**Alternatives — who shares an entry.** (1) One global namespace: the best hit
rate, and the second tenant to send a prompt gets text generated under the first
tenant's key. Exact-match means they had to send that prompt themselves, so
nothing leaks *in*, but the answer is still another customer's completion.
(2) Namespace by `key_id`. (3) Namespace by an operator-declared tenant.
Chose (2), with (1) behind `VORTEX_CACHE_SCOPE=global` for a single-tenant
deployment where the hit rate is the whole point. `key_id` is already the thing
the limiter and the ledger are keyed on, so the cache inherits an identity that
survives restarts and is safe in a Redis key (ADR-003).

**Alternatives — streams.** (1) Accumulate the chunks, store the assembled
completion, replay it as a synthetic stream on a hit. (2) Serve a cached
buffered response by re-chunking it. (3) Bypass streaming requests entirely.
Chose (3) for now — see ADR-004. (1) and (2) are the same feature and it is not
small: a stream has three endings (ADR-019) and two of them, failed and
abandoned, produce a *partial* answer that must never be written, so the cache
would have to learn the ending taxonomy that `streaming.py` owns. The header
says `BYPASS` rather than `MISS`, because "we did not look" and "we looked and
it was not there" are different facts and only one of them is a cache to tune.

**Alternatives — what a hit costs.** A hit consumed no upstream tokens but it
did consume a request. (1) Skip metering: a cache becomes a rate-limit bypass,
and a caller who can produce hits can produce unlimited ones. (2) Meter it as
if the provider ran: the ledger then reports token spend against an invoice
line that does not exist, and ADR-022 exists so the ledger can be reconciled
against the vendor's bill. (3) Admit it normally — one request against RPM —
then settle it at zero tokens, so the reservation comes back and the ledger
records a request that cost nothing. Chose (3).

**Bypass reads *and* writes.** `X-Vortex-Cache-Bypass: true` skips the lookup
and the store. A bypass that still wrote would be a refresh, which is a useful
thing and a different one: an operator comparing the cached answer against a
fresh one would destroy the entry they were comparing against.

**Planned experiment.** Send the same request twice through a metered gateway
and read `provider.call_count`. Prediction: 1, with the second response
byte-identical to the first, `X-Cache: HIT`, and a ledger showing two requests
and one request's worth of tokens. The number to watch is the ledger: if the
hit records the cached response's usage, the second row will carry tokens
nobody bought, and it will look right in every test that only reads the header.
