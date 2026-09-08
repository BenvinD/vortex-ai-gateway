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
Consequences: Every new dependency (Redis, providers) must register a readiness
check or it is invisible to draining. Revisit if we adopt a service mesh that
owns readiness gating itself.
