# ADR-018: Usage is collected on every stream, forwarded only on request
Date / Status: 2026-09-03 / accepted
Context: On a stream, token counts arrive in a final chunk that providers send
only when `stream_options.include_usage` was set. Callers rarely set it, so the
gateway was left with no record of what it had just paid for — and cost
accounting needs the bill for every request, not the ones a caller opted into.
Options: A) collect usage only when the caller asks, and accept blind spots
B) estimate at the edge by counting relayed deltas C) always ask the provider
for usage; let `include_usage` decide only whether the chunk is *forwarded*.
Decision: C. `streaming.metered()` copies the request with `include_usage` set
before it reaches the provider, and `StreamRecord.observe()` strips the usage
chunk back out when the caller did not ask — dropping it entirely when it
carries nothing else. The provider seam keeps its meaning; the gateway is simply
a client that always opts in, because it is the party being billed. (B) was
rejected because prompt tokens are invisible at the edge and a relayed-delta
count would disagree with the invoice for the half it can see.
Consequences: The request a provider receives is not byte-for-byte the one the
caller sent, which is visible in `MockProvider.received_requests`. Providers that
report usage only at the very end still yield nothing for an abandoned stream —
see ADR-019. Revisit if a provider starts charging for usage reporting, or if
per-chunk usage becomes standard enough to meter incrementally.
