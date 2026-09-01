# ADR-009: The unified contract is OpenAI's chat-completions shape
Date / Status: 2026-09-01 / accepted
Context: A multi-provider gateway needs one vocabulary at its edge. Callers must
not rewrite their request when routing moves from OpenAI to Anthropic to Gemini,
and adapters need a fixed target to translate to and from.
Options: A) a bespoke Vortex schema, cleaner than any vendor's B) OpenAI's
`/v1/chat/completions` request/response as the unified contract C) a
lowest-common-denominator subset supported by every provider.
Decision: B. `vortex_ai_gateway.contracts` defines the whole wire format as
pydantic models — messages, tools, options, usage, chat, catalog — depending on
nothing else in the codebase, so routing, adapters and tests share one source of
truth. Every existing OpenAI SDK works by changing only the base URL, which is
the entire adoption argument. One additive extension, the `vortex` object on a
response, records which provider and upstream model served the call.
Consequences: We inherit OpenAI's warts (`arguments` as a JSON string, the
`max_tokens`/`max_completion_tokens` split) and track their schema changes.
Provider features with no OpenAI spelling need an explicit extension field
rather than a natural home. A (bespoke) wins if the gateway ever stops being a
drop-in replacement; C loses because clamping to the intersection would drop
tool calling and streaming, which is most of the value.
