# ADR-019: A client disconnect cancels the upstream stream, and is recorded
Date / Status: 2026-09-03 / accepted
Context: A client that hangs up mid-generation is the one failure that costs
money without producing anything. The access log cannot see it: it records
`200` and a short duration, which is exactly what a fast successful stream looks
like. And if the upstream request is not torn down, tokens keep being generated
and billed for an answer nobody will read.
Options: A) rely on the upstream response being closed when the abandoned
generator is garbage-collected B) poll `receive()` for `http.disconnect` in the
route C) let the cancellation Starlette already raises propagate into the
provider's generator, catch it only to record the abandonment, and re-raise.
Decision: C. `_stream_events` names `CancelledError`/`GeneratorExit` explicitly,
marks the `StreamRecord` `abandoned`, and re-raises so teardown continues into
the adapter's `finally`, which closes the upstream connection. Every stream logs
one `stream finished` record — completed, failed or abandoned — which is the
seam cost accounting hangs off. (B) duplicates what `StreamingResponse` does and
only works where the server delivers the message.
Consequences: Correct teardown depends on the cancellation actually arriving.
Starlette only listens for `http.disconnect` when the server advertises ASGI
spec < 2.4 (uvicorn advertises 2.3); on a 2.4 server it relies on `send()`
raising instead, and an abandoned generator falls back to (A)'s garbage
collection — hence the belt-and-braces `aclose_stream` in the relay's `finally`.
An abandoned stream is recorded with its chunk count but usually no token
counts, because most providers report usage only in a final chunk that never
arrives.
