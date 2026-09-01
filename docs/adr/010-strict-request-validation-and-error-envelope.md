# ADR-010: Reject unknown request fields; answer in OpenAI's error envelope
Date / Status: 2026-09-01 / accepted
Context: A gateway that silently ignores a misspelled parameter charges the
caller for a request that did not do what they asked, and the bug surfaces as a
quality complaint weeks later. Failures also have to be legible to clients
written against OpenAI's error format.
Options: A) `extra="allow"`, forwarding unknown keys to the provider
B) `extra="ignore"`, the pydantic default C) `extra="forbid"` on every contract
model, plus cross-field validators and a translated error envelope.
Decision: C. `ContractModel` sets `extra="forbid"`, so `temperture` is a 400
naming the key. Semantic checks that no single field can make live on the
request: conflicting token limits, `top_logprobs` without `logprobs`, a forced
tool that was never declared, a tool result with no matching call. Failures
return `400` with `{"error": {message, type, param, code}}`, where `param` is
the pydantic location rendered as a key path (`messages[0].content`) with the
transport prefix and union bookkeeping stripped.
Consequences: A parameter OpenAI adds tomorrow is rejected until it is declared
here — accepted, since the alternative is silently dropping it. FastAPI's `422`
is remapped to `400` to match OpenAI. A wins if the gateway ever needs to be a
transparent proxy for provider-specific extras.
