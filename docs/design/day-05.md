## Day 05 — streaming that the gateway can account for

**Building.** The streaming path already relays chunks; what it cannot do is
say what a stream *cost* or notice that nobody was listening. Two gaps, both of
which are money:

1. Token usage on a stream arrives in a final chunk that the provider sends
   only when `stream_options.include_usage` was requested. A caller who does
   not ask leaves the gateway with no bill for the request it just paid for —
   and Day 6 is cost accounting, which needs the bill for *every* request.
2. A client that hangs up mid-generation is invisible. The access log records
   `200` and a short duration, which is indistinguishable from a stream that
   finished quickly. If the upstream request is not also torn down, tokens keep
   being generated — and billed — for an answer nobody will read.

**Alternatives — usage.** (1) Ask the provider for usage only when the caller
does, and accept that most requests have no bill: cheapest, and gives up on
cost accounting. (2) Estimate tokens at the edge by counting the deltas the
gateway relayed: works for every provider, but it is an estimate that will
disagree with the invoice, and prompt tokens are unknowable this way. (3) Ask
the provider for usage on *every* stream and let `include_usage` decide only
whether the usage chunk is forwarded to the caller. Chose (3) — see ADR-018.
The gateway is the party being billed, so usage is its telemetry first and the
caller's data second.

**Alternatives — disconnects.** (1) Do nothing and rely on the upstream request
being torn down when the response object is garbage-collected: probably prompt
under CPython refcounting, and not something to guess about when the failure
mode is a bill. (2) Poll `receive()` for `http.disconnect` in the route and
break the loop: duplicates what Starlette's `StreamingResponse` already does,
and only works on servers that deliver the message. (3) Let the cancellation
Starlette raises propagate into the provider's generator — whose `finally`
closes the upstream connection — catch it to record the abandonment, and
re-raise. Chose (3) — see ADR-019.

**Planned experiment.** Run the gateway against an upstream that narrates every
token it generates, start a `curl -N`, kill the client mid-stream, and count how
many tokens the upstream generated after the client was gone. The prediction is
that it generates all of them: nothing in the current code closes the upstream
response, and the `except Exception` in the relay would swallow a cancellation
if `CancelledError` were an `Exception` (it is not, which may be the only reason
this works at all).
