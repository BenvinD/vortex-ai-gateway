# ADR-020: Fallback is an explicit chain in configuration, not implicit rescue
Date / Status: 2026-09-06 / accepted
Context: With a breaker in place a provider outage turns into a fast, honest
`503` — but the gateway's promise was that it routes around exactly this. The
question is who decides where the traffic goes, and on which failures.
Options: A) no fallback; a `503` is honest B) automatic failover on any error
C) ordered chains named in config, hopped only on failures that mean *this*
provider cannot serve the call.
Decision: C, as `VORTEX_FALLBACK_CHAINS=primary>next>last`, parsed and validated
at boot like the routing table: an unknown provider, a chain that loops, two
chains with the same head, or a head no routing rule reaches all stop the
gateway starting. A hop happens on `CircuitOpenError` or on a retryable failure
that exhausted its retries — never on a `ProviderBadRequest`, which would fail
everywhere and would spend a second provider's quota to return the same `400`
more slowly. `UnsupportedParameterError` is also not hopped, though it is the
one case where another provider genuinely would succeed; rescuing it silently
would change which model answers a request that named one, so it stays a routing
decision a human makes.
Consequences: An outage is now a logged hop instead of an error, and
`response.vortex.provider` still names whoever actually answered. The request is
forwarded **unchanged**, so a chain is only valid between providers that answer
to the same model names — OpenAI-compatible endpoints, or aliased deployments.
Per-hop model rewriting is the obvious next step and is deliberately not built:
it needs syntax, and this needed shipping. Streams do not fall back, for the
same reason they do not retry.
