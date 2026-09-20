# ADR-004: Exact response cache, keyed per caller, with streams bypassing it
Date / Status: 2026-09-08 / accepted
Context: Identical requests are common — a retry, a re-render, a test suite, a
prompt template with no variable part — and every one of them currently costs a
provider call and a token bill. A cache is the cheapest latency and cost win
available, and it is also the first subsystem that lets one caller observe an
answer generated for another, so what shares an entry is a correctness question
before it is a hit-rate one.
Options: A) hash the raw request body B) hash the validated request, dumped
canonically C) similarity matching on the prompt. And for what shares an entry:
one global namespace, or one per `key_id`. And for streams: replay stored
chunks, or bypass.
Decision: B, per key, streams bypass. The raw body makes a client library's JSON
key order part of the cache key, so an ordering change halves the hit rate and
nothing reports it; hashing `model_dump(mode="json")` with `sort_keys` instead
means field order stops mattering and an omitted parameter hashes as its
default. `stream`, `stream_options`, `user` and `metadata` are dropped before
hashing because none of them can change a generated token; everything else is in
the key, `seed` and `n` included. C is Day 10's job and needs an embedding model
and a threshold (ADR-005). Entries are namespaced by `key_id` because an exact
hit returns a completion generated under — and billed to — another tenant's key;
`VORTEX_CACHE_SCOPE=global` opts a single-tenant deployment back into the shared
hit rate. Streams bypass in both directions and are reported as `BYPASS` rather
than `MISS`, because "we did not look" is not a hit rate worth tuning: two of a
stream's three endings (ADR-019) produce a partial answer that must never be
stored, so caching one means teaching the cache the ending taxonomy that
`streaming.py` owns. A hit is admitted through the limiter like any other
request and then settled at *zero tokens*, so the reservation comes back and the
ledger counts a request that bought nothing — recording the cached response's
usage would put spend against an invoice line that does not exist (ADR-022).
`X-Vortex-Cache-Bypass` skips the lookup *and* the store, so debugging the cache
cannot change what is in it. Redis failures fail open, as in ADR-021.
Consequences: Repeated requests are free and instant, and `X-Cache` makes which
happened visible per response. Within a route's TTL an identical request cannot
produce a different sample, so a caller relying on `temperature` for variety
gets none until it expires — the TTL is the knob, per route. Per-key namespacing
multiplies storage by the number of callers sending the same prompts, which is
the price of not serving one tenant's completion to another. The rejected
streaming option wins as soon as streams dominate traffic and repeat: the answer
is then to accumulate chunks and store only on the *completed* ending, never on
failed or abandoned. Counters are per worker process, like the breakers, and
become fleet-wide the same day the breakers do.
