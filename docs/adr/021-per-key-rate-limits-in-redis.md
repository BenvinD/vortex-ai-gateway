# ADR-021: Per-key RPM and TPM as one atomic token bucket in Redis
Date / Status: 2026-09-06 / accepted
Context: Every authenticated caller could send as much as it liked. One client's
retry storm becomes the gateway's bill and every other client's latency, and the
breakers from ADR-002 only notice once a provider is already suffering. Limits
need to be per key (ADR-003 made keys durable), shared across workers, and cover
both dimensions that cost money: request count and token count.
Options: A) per-process counters B) Redis `INCR` on a per-minute key C) a token
bucket in a Lua script, both buckets checked and debited in one call.
Decision: C. Per-process is not a limit — N workers means N times the limit. A
fixed window admits twice the limit across the minute boundary and cannot
express "admit only if *both* allowances fit". Atomicity is the requirement, not
a nicety: with three round trips a request refused by the token bucket has
already spent a request. TPM is a limit on a number that does not exist yet, so
admission reserves an estimate (`max_completion_tokens`, else `len(text)//4`
plus a configured assumption) and settlement reconciles it against real usage;
the refund is clamped at capacity so an over-estimate cannot overfill a bucket.
`now` is passed in as an argument rather than read with `TIME` inside the script,
which keeps it deterministic and testable. Zero means unlimited, so a gateway
with no limits configured never opens a Redis connection at all. Redis failures
**fail open** with one warning, and Redis is deliberately *not* a readiness
check: draining a working instance because the limiter is degraded turns a
degradation into an outage.
Consequences: Limits hold across workers and machines, and the 429 carries
`Retry-After` plus the `X-RateLimit-*` sextet on success as well as rejection, so
a client can slow down before it is throttled rather than after. The estimate is
crude, so a caller with unusually long completions is admitted slightly too
freely until settlement corrects it — acceptable, because the reconcile is what
makes the limit mean tokens. Fail-open means a Redis outage is an unlimited
gateway; the rejected option wins if the limits ever become a contractual quota
rather than a protection, and then the answer is fail-closed with a much louder
alarm, not a readiness probe.
