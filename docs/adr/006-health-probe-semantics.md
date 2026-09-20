# ADR-006: Health probe semantics
Date / Status: 2026-08-31 / accepted
Context: Orchestrators need two distinct signals — "restart this process" and
"stop routing to this process" — and conflating them turns a dependency blip
into a restart storm.
Options: A) single `/health` that checks dependencies B) `/healthz` liveness +
`/readyz` readiness, split by what a failure should trigger C) no probes, rely
on TCP checks.
Decision: B. `/healthz` checks nothing external and means "do not restart me";
`/readyz` runs registered dependency checks and returns 503 (per-check
breakdown) so the load balancer drains the instance without it being killed.
Consequences: Every new dependency must be considered for a readiness check or
it is invisible to draining. Revisit if we adopt a service mesh that owns
readiness gating itself.
Update (2026-09-08): "considered for", not "must register" — ADR-021 refined the
test. A dependency belongs in `/readyz` only if the instance cannot serve useful
traffic without it. Redis deliberately does not qualify: the limiter, ledger and
cache all fail open, so draining a working instance over a degraded Redis would
turn a degradation into an outage. `readiness_checks` is consequently still
empty, which is a correct state and not an oversight.
