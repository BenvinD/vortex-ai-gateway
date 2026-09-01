# ADR-011: A two-method provider seam, with a scripted mock as the first implementation
Date / Status: 2026-09-01 / accepted
Context: Routing, retries, streaming and error mapping all need *a* provider to
exercise them, and none of them should require a vendor key, a network, or a
per-run bill. The seam those layers code against has to exist before the first
real adapter, or it gets shaped by whichever vendor was integrated first.
Options: A) build the OpenAI adapter first and generalise later B) define a
narrow `ChatProvider` protocol (`complete`, `stream`) and satisfy it first with a
scripted `MockProvider` C) test against recorded HTTP fixtures (VCR-style).
Decision: B. `ChatProvider` is a `Protocol`, so adapters are structurally typed
with no base class to inherit; implementations raise on failure and never build
an error envelope, keeping HTTP status choices in the routing layer.
`MockProvider` replays a script — where an entry may be an `Exception`, which is
how retry and breaker paths get tested — and derives a consistent reply when
unscripted, honouring `n`, the output cap, `tool_choice` and `response_format`.
IDs, timestamps and word-based token counts are deterministic, so responses can
be asserted whole.
Consequences: The mock's behaviour is a second thing to keep true to the
contract; where it drifts from a real provider, tests lie. C stays the right
tool for verifying one adapter's translation against real vendor bytes — that is
a per-adapter concern, not a substitute for this seam. The protocol will widen
(model listing, embeddings, health) as adapters land.
