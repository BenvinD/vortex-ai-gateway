# ADR-028: `/metrics` is published open, beside the health probes and outside `/v1`
Date / Status: 2026-09-22 / accepted
Context: Prometheus scrapes with a bare HTTP GET on a timer. It has no API key,
and the gateway's keys are minted for callers, carry per-key rate limits, and
are revocable one at a time (ADR-003) — none of which describes a scraper. The
`/v1` router declares `Depends(require_api_key)` at the router level precisely
so a route added later cannot be published unauthenticated by omission, so
where the endpoint is mounted *is* the authentication decision.
Options: A) mount it under `/v1`, behind the key, and issue the monitoring
system a credential B) mount it on the app beside `/healthz` and `/readyz`,
open C) mount it open but on a second port bound to the internal interface
D) leave it out and export metrics over OTLP to the same collector the traces
go to.
Decision: B, with a switch (`VORTEX_METRICS_ENABLED`) and a configurable path.
A fails on its own terms: the credential that can read your traffic volumes then
lives in a monitoring system's configuration file, gets copied into every
environment, and is the one key nobody dares revoke. C is right and is the
orchestrator's job, not this process's — a second uvicorn port inside the app
would need its own lifecycle and would still be reachable from wherever the
first one is. D was rejected because a scrape needs no agent, no collector and
no delivery guarantee to be useful, and "is the gateway up and what is it
doing" should not depend on a pipeline being healthy. The endpoint is safe
because it is boring: counts, latencies, token totals and dollar totals, and
never a prompt, a completion, a key or a `key_id`. The counters are updated
whether or not the endpoint is mounted, because an atomic increment is cheaper
than the branch that would skip it.
Consequences: A gateway exposed directly to the internet leaks its request
rate, model mix and spend to anyone who asks, so such a deployment sets
`VORTEX_METRICS_ENABLED=false` and scrapes a sidecar — the README says so. What
is published is per *worker process*, like the breaker's state and both caches'
counters: one worker per port, and scale with containers.
`prometheus_client`'s multiprocess mode is deliberately not wired up; it needs a
shared directory, it changes what a gauge means, and it would not fix the other
per-worker state. The `_created` companion series are switched off, since
Prometheus's text scrape ignores them and they double the series count this ADR
and ADR-027 exist to watch. Revisit the moment a metric would carry something
about a *caller* — a `key_id` label — because that is the point at which the
endpoint stops being boring and A becomes the right answer after all.
