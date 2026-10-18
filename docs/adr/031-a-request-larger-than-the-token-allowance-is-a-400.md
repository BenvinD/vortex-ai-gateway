# ADR-031: A request larger than the whole token allowance is a `400`, not a `429`
Date / Status: 2026-10-18 / accepted
Context: ADR-021 reserves an estimate against a token bucket whose level never
exceeds its capacity. A request estimated above the key's whole TPM therefore
can never be admitted — yet it got a `429` with a finite `Retry-After`, which a
client obeys, and is refused again, forever. It is not a corner: the estimate
adds `rate_limit_assumed_completion_tokens` (512) when a caller sets no
`max_completion_tokens`, so a key minted with `--tpm 500` refused every such
request while promising each time that two seconds would fix it.
Options: A) keep the `429` and drop only its `Retry-After` B) cap the hold at
the bucket's capacity and admit it once the bucket is full C) let the bucket go
into debt — admit when full, settle below zero — so the caller waits it off
D) a `400` the caller can act on.
Decision: D, as `invalid_request_error` with code `request_exceeds_token_limit`,
no `Retry-After`, and a message naming the estimate, the limit and what to
shrink. It is decided in `Meter.admit` from the key's configuration before
Redis is asked, so it costs nothing from the request bucket, and the answer is
the same when Redis is down and the limiter is failing open. A keeps a status
every OpenAI SDK retries by itself, twice by default, so each oversized request
becomes three refusals. B serves the big-`max_tokens` request that uses little,
but settlement clamps at zero, so a caller whose every request exceeds TPM gets
one per window and sustains many times its limit. C is the only option that
makes a `Retry-After` true, and it changes both Lua scripts for a debt that is
mostly fictional until settlement, because the estimate is built from an upper
bound.
Consequences: The estimate, which ADR-021 calls deliberately crude, now has
the power to refuse outright, but only above the whole allowance, and the
message says how to get under it. A request exactly at capacity is still
admitted. Not built: a warning from `vortex-keys create` when `--tpm` is below
the assumed completion, which would catch the misconfigured key before its
first request. Revisit if callers routinely send `max_tokens` far above what
they use: then C's debt is the honest answer, and worth two scripts.
