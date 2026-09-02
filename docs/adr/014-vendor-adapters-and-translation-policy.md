# ADR-014: Vendor adapters translate both ways, and refuse what they cannot express
Date / Status: 2026-09-02 / accepted
Context: OpenAI, Anthropic and Ollama disagree about message structure, where
sampling parameters live, and which of them exist at all. The gateway promises
one OpenAI-shaped contract (ADR-009), so every one of those differences has to
be resolved somewhere — and each one resolved *silently* is a behaviour change
the caller cannot see in a well-formed response.
Options: A) translate what maps and drop the rest B) translate what maps and
raise on anything the vendor cannot honour C) narrow the contract to the
intersection of all three providers.
Decision: B, in one adapter per vendor behind the existing two-method
`ChatProvider` seam; `HttpChatAdapter` holds the shared transport but is not
the seam, so adapters stay structurally typed (ADR-011). `n`, `seed`,
`logprobs`, `logit_bias`, the penalties and a forced `tool_choice` raise
`UnsupportedParameterError` where the vendor has no equivalent; `temperature`
is clamped to each vendor's range; a model alias resolves to the vendor's ID,
with the caller's alias returned in `model` and the real one preserved in
`vortex.upstream_model`.
Consequences: A caller learns at once that a parameter did nothing, rather than
trusting an answer to a question it did not ask — but a request that is
portable on OpenAI is an error on Anthropic, so cross-provider routing will
have to degrade parameters deliberately rather than hope. Translation is
deliberately asymmetric: *responses* are lenient, skipping content blocks we do
not model (`thinking` today), because an additive block should not cost the
caller the answer, while an unmodelled *field* still fails loudly (ADR-010). C
was rejected because the intersection of three providers is no longer a
drop-in replacement for any of them.
