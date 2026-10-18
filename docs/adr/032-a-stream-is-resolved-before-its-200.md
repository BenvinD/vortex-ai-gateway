# ADR-032: A stream is resolved before its `200`; after it, failures are events
Date / Status: 2026-10-18 / accepted
Context: ADR-012 said a streaming failure "cannot be" a status, because the
`200` is already on the wire, and made it an in-band error event. That is true
only after the status line is sent, and nothing recorded *when* that happens.
The router raises an unroutable model synchronously from `stream()` precisely so
that it can still be a `404`, but `routes.py` called `stream()` inside the SSE
body generator, which runs after the status line and before its own
`try`/`finally`. So the caller got `200` with an empty body, the server logged a
traceback, and the token reservation was never released.
Options: A) hold the `200` until the first chunk arrives, so every failure
before the first token is a status B) widen the seam with a `resolves(model)`
the route asks first C) call `stream()` in the route, before the
`StreamingResponse` is built.
Decision: C. The boundary is the route returning: admission, both cache tiers
and the call to `stream()` happen before it and fail as statuses through one
helper, `_failed`, shared with the buffered path, which logs, releases the
reservation in full and renders the envelope. Everything after the first byte
is an event, as ADR-012 decided. A is the most capable option and is not built:
it moves time-to-first-token, the abandon path and cancellation into the request
handler, and it is the first half of the establishing-phase retries ADR-001 names
as its own revisit trigger, so it belongs with them rather than inside a bug
fix. B widens `ChatProvider` for one caller, which ADR-011 rules out, and a
`stream()` that raises would still need catching.
Consequences: An unroutable stream is a `404` that spends nothing, like its
buffered twin. Only failures raised synchronously by `stream()` move across the
line; an adapter's stream is a generator, and its upstream `5xx` still arrives
as an event after a `200`. Revisit with A when streams get establishing-phase
retries, or when a client depends on telling an upstream failure from success by
status alone.
