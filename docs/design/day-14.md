## Day 14 — places the gateway told someone something untrue

Part one is the breaker; part two, written after part one landed and before
its own code, is three more that turned up beside it; part three closes the
two gaps part two left open.

### Part one — making sure the half-open trial always reports back

**Building.** ADR-002's breaker lets exactly one request through as a trial
once the open window has passed, and its docstring says the slot "must always
be handed back — or the breaker would sit in `HALF_OPEN` refusing everything
forever". Day 6 handed it back on cancellation and nowhere else. Three other
ways out of `ResilientProvider.complete` keep it: (A) the trial fails
retryably, the loop retries, and the trial's own second `allow()` is refused by
its own reservation; (B) the trial fails non-retryably and the bare `raise`
skips the release; (C) the trial raises something that is not a
`ProviderError`. Each leaves the breaker in `HALF_OPEN` with no clock running,
permanently, until the worker restarts. A is the one that matters, because it
fires on the expected case: the provider is still down at the first probe.

**Alternatives — a trial that fails retryably.** (1) Let the trial keep its
retries, holding the slot across its own attempts: a blip during the probe
does not double the window, but every other caller is refused while the trial
sleeps through backoff for up to the 90-second deadline, and a still-dead
provider takes three calls per window instead of one. (2) Hand the slot back
between attempts: the next caller can then take it, so a probe becomes two
concurrent calls and the trial's own retry is refused anyway. (3) The trial is
one call; a retryable failure reopens the breaker immediately, for the
escalated window. Chose (3) — see ADR-030. ADR-002 already said recovery is
probed by "exactly one request"; this makes it exactly one *call*, which is
what "a trickle rather than the herd" needs. The caller also gets the real
`502`/`504` back instead of a `503` with no `Retry-After`.

**Alternatives — a trial that learned nothing.** For a non-retryable failure
and for an exception that is not a `ProviderError`: (1) count it as a failed
trial: simplest, and one client's malformed request at the wrong moment extends
an outage for everyone, which ADR-002 forbids in `CLOSED`; a deterministic
adapter bug would cycle the breaker open at escalating windows forever. (2) For
non-retryable errors, treat any answer from the vendor as proof of life and
close: faster recovery, but `TranslationError` is raised before the wire, so
it needs a "did this reach the upstream" flag the taxonomy does not carry.
(3) Hand the slot back without a verdict, as cancellation already does. Chose
(3) for both: nothing was learned about the upstream, and the next caller
becomes the trial.

**The awkward part: who is allowed to hand the slot back.** `release_trial()`
clears the slot for whoever calls it. A long completion admitted while
`CLOSED` that ends during `HALF_OPEN` would free the *real* trial's slot and
let a second trial run — and fixing B and C puts a release on every exit, so
that stops being a corner. And the wrapper cannot implement (3) above without
knowing whether its call *is* the trial, which a bare `bool` from `allow()`
does not say. Three ways out. (1) Keep the `bool` and read `state()` after
`allow()`: a second lock round trip, and it still cannot tell two holders
apart. (2) An `async with breaker.admit()` context manager that releases on
any exit without a verdict: structural, and it moves the retry loop's verdict
calls inside a block per attempt. (3) `allow()` returns a `Permit | None`; the
permit says whether it is the trial, and `release_trial(permit)` is a no-op
unless that exact permit still holds the slot. Chose (3): ownership by
identity, one `finally`, and the existing tests change from `is True` to
`is not None`.

**Planned experiment.** Rerun the three-path repro and add the cross-release
case: a call admitted in `CLOSED`, still running when the breaker has opened,
waited out its window and handed the trial to a second call, which then gets
cancelled. Prediction: before the fix, all three paths wedge and the
cross-release lets a third caller in while the real trial is still running;
after it, A makes one upstream call and leaves the breaker `OPEN` for 20 s
(the escalated window), B and C leave it `HALF_OPEN` with the slot free, and
the third caller is refused. The number to watch is A's upstream call count:
three would mean the trial is still retrying.

### Part two — two refusals that lie, and a counter that counts twice

**Building.** Three more, each telling a caller or a dashboard something
untrue. (1) A streamed request for a model no rule routes gets `200` with an
empty body, and its token reservation never comes back: 1,001 tokens per
request in the repro, until the window refills. The router raises
synchronously from `stream()` precisely so that this can still be a `404`, but
`routes.py` calls `stream()` inside the SSE body generator, which runs after
the status line and before that generator's own `try`/`finally`. (2) A request
whose token estimate exceeds the key's whole TPM allowance is refused with a
finite `Retry-After` that can never come true: the bucket's level is capped at
capacity, so waiting cannot help. A key minted with `--tpm 500` refuses every
request that does not set `max_completion_tokens`, because the assumed
completion alone is 512. (3) A give-up on budget or deadline is counted under
its own reason *and* under `attempts exhausted`, and logged twice, the second
time claiming three attempts after one.

**Alternatives — a request larger than the whole allowance.** (1) Keep the
`429` and drop only the `Retry-After`: the smallest change, and every OpenAI
SDK retries a `429` on its own, twice by default, so each oversized request
becomes three refusals. (2) Cap the hold at capacity, admitting it once the
bucket is full: it serves the large-`max_tokens` request that uses little, but
settlement clamps at zero, so a caller whose *every* request exceeds TPM gets
one a window and sustains many times its limit. (3) Let the bucket go into
debt, admitting it when full and settling below zero: sound on average and the
only option that makes a `Retry-After` true, but both Lua scripts change and
the debt is mostly fictional until settlement, because the estimate is built
from an upper bound. (4) A `400` the caller can act on, `invalid_request_error`
with code `request_exceeds_token_limit`, naming the estimate and the limit.
Chose (4) — see ADR-031. It is decided from the key's configuration before
Redis is asked, so it does not cost a request from the RPM bucket. That also
means the answer stays the same when Redis is down, where the fail-open limiter
would otherwise admit a request it would refuse a minute later.

**Alternatives — an unroutable stream.** (1) Hold the `200` until the first
chunk arrives, so every failure before the first token gets a real status: by
far the most capable option, and it moves time-to-first-token, the abandon
path and cancellation into the request handler, which is a day of its own.
(2) Give the seam a `resolves(model)` the route asks first: explicit, and it
widens `ChatProvider` for one caller, which ADR-011 says not to do. (3) Call
`stream()` in the route, before the `StreamingResponse` exists, so the router's
synchronous raise lands where the buffered path's failures already do. Chose
(3) — see ADR-032, which writes down the boundary this restores: everything
decided before the route returns is a status, everything after is an event.

**The awkward part: the double count is in a function that returns a
`bool`.** `_should_retry` both decides and reports. It counts and logs its own
reason, then returns `False`, and the code after the loop cannot tell
"`_should_retry` already reported this" from "the attempts ran out", so it
reports again. Two ways out: have the code after the loop count only when
nothing else did, which keeps two log lines per give-up; or have the function
*return the reason* and do no reporting, so the one place that gives up writes
one line and one counter with the reason and the attempts actually made. The
second, because a give-up is one event.

**Planned experiment.** Rerun the refusal repro. Prediction: the unroutable
stream becomes a `404` with code `model_not_found` and the bucket reads the
same after three of them as before; the oversized request becomes a `400` with
no `Retry-After` while a request exactly at capacity is still admitted; and
each give-up reason reads 1 with every other reason at 0. The number to watch
is the bucket after the unroutable streams. If it still drops, the reservation
is being taken somewhere the new failure path does not reach.

### Part three — a stream that waits for its first chunk, and a key that warns

**Building.** Part two left two gaps, both written down rather than fixed.
(1) An upstream `5xx`, `429`, timeout or bad key on a stream still arrives as an
in-band error event after a `200`, because the route returns the
`StreamingResponse` as soon as `stream()` hands back a generator, and the
adapter only makes its HTTP request when that generator is first iterated. And
none of those failures is retried or hopped: `ResilientProvider.stream` and
`FallbackProvider.stream` forward the head's stream untouched, so the breaker
never sees a stream at all. (2) `vortex-keys create --tpm 500` mints a key that
refuses nearly every request (ADR-031), and says nothing.

**Alternatives — when a stream commits to its `200`.** (1) When the route
returns, as part two left it: a status for anything `stream()` raises
synchronously, which is only routing. (2) At the first chunk with content: also
catches a vendor that accepts and then fails before generating, but holds the
status line through a reasoning model's whole think time. (3) At the first
chunk of any kind: for OpenAI and the mock that is the role-only chunk, which
already proves the request was accepted — auth, model, quota and `5xx` all
surface before it. Chose (3) — see ADR-032, rewritten in place for this.
No new timeout: a hung upstream already becomes `ProviderTimeout` from the
adapter's own read timeout, so the wait is bounded by a setting that exists.

**Alternatives — what a failure before the first chunk gets.** (1) A status
code only, streams still outside the resilience wrappers: the smaller change,
and it leaves the one window in which a stream *can* be retried unused. (2) The
same retries, breaker and fallback hops `complete()` gets, all before the first
chunk. Chose (2). Nothing has been sent, so a retry or a hop splices nothing —
ADR-001's and ADR-020's reason for keeping streams out was about *after* the
first chunk, and they named this as the condition to revisit. The breaker's
verdict on a stream is the first chunk: arriving is a success, failing to
arrive after the retries is a failure, and a failure *after* it moves nothing,
because by then the upstream has demonstrably answered.

**The awkward part: the retry loop has to live in two places, or be shared.**
`ResilientProvider.complete` owns the loop — the permit, the trial rule, the
`finally` that hands a slot back, the backoff, the single give-up report — and
it took a day to make that loop correct. Copying it into `stream()` would make a
second loop that drifts from the first. So the loop becomes generic over what
one attempt *is*: for `complete()` it awaits the completion; for `stream()` it
opens the stream and awaits its first chunk, returning both. The streaming
wrapper is then a generator that calls the shared loop, yields the first chunk
and relays the rest, closing the inner stream in a `finally` so a hang-up still
reaches the adapter (ADR-019). `FallbackProvider` gets the same treatment: one
loop over the chain, generic over the attempt. The cost is one more generator
layer per wrapper between the client and the adapter, which the abandonment
tests have to prove does not stop a cancellation reaching the upstream.

**The key warning.** No alternatives worth pricing: ADR-031 named it. It
prints to stderr after the token, so a script piping the token still gets only
the token; it never refuses, because an operator may want a tiny key on
purpose; and it checks the *effective* limit, so `--tpm 0` is judged by the
deployment default it will inherit.

**Planned experiment.** A scripted upstream that fails `503` twice and then
streams. Prediction: three upstream calls, two backoffs, then a `200` whose
body is the third call's chunks. With the upstream failing every time, a `502`
with an error envelope and the reservation back in full, not a `200`. A stream
that fails after its first chunk is not retried: one call, a `200`, the error
as the final event. The number to watch is the upstream call count on the
mid-stream failure. Two would mean the retry loop is running past the first
chunk and splicing generations.
