# ADR-032: A stream commits to its `200` at the first chunk, and is retried until then
Date / Status: 2026-10-18 / accepted
Context: ADR-012 said a streaming failure "cannot be" a status, because the
`200` is already on the wire, and made it an in-band error event. That is true
only after the status line is sent, and nothing recorded *when* that is. The
route returned the `StreamingResponse` as soon as `stream()` handed back a
generator, and an adapter makes its HTTP request only when its generator is
first iterated — so a refused key, a `429`, a `5xx` or a timeout all arrived
after a `200`. Worse, the router's synchronous `UnroutableModelError` was raised
inside the SSE body, after the status line and outside its `finally`: a `200`
with an empty body and a reservation never released. And because ADR-001 and
ADR-020 kept streams out of the resilience wrappers entirely, none of those
failures was retried or hopped, and the breaker never saw a stream.
Options: A) commit when the route returns, with `stream()` called first so a
routing failure is a status B) commit at the first chunk with *content*
C) commit at the first chunk of any kind, retried and hopped until then
D) C without the retries and hops — statuses only.
Decision: C. The route awaits the first chunk before building the response;
for OpenAI and the mock that is the role-only chunk, which already proves the
request was accepted. The wrappers run the same loop for a stream as for a
completion, generic over what one attempt is — here, opening the stream and
awaiting its first chunk — so the permit, the one-call trial, the backoff and
the single give-up report (ADR-030) cannot drift between the two shapes. The
first chunk is the breaker's verdict: it arriving is a success; a failure after
it moves nothing and is never retried or hopped, because a second attempt would
splice two generations into one body. Failures before it go through one helper,
`_failed`, shared with the buffered path. A, which this ADR first chose, leaves
every upstream failure an event and the one window in which a stream *can* be
retried unused. B holds the status line through a reasoning model's whole think
time. D gives statuses and wastes the window. No new timeout: the adapter's
read timeout already turns a silent upstream into a `504`.
Consequences: A stream against a flaky upstream is a `200` with the attempt
that worked; against a dead one, a `502`, then a `503` with `Retry-After` once
the breaker opens — where it was a `200` and an error event every time. Every
wrapper is now a generator layer that must close the stream it holds in a
`finally`, or a hang-up at a `yield` leaves the adapter for the garbage
collector; a unit test pins that, because an end-to-end hang-up cannot see it.
A client that hangs up during the wait is not noticed until the first chunk,
since nothing listens for a disconnect before the response exists; a
cancellation that lands there is recorded as abandoned. Revisit if a vendor's
first chunk stops meaning acceptance — one that sends an opening event before
it has validated the request — then B.
