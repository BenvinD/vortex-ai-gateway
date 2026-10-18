## Day 14 — four places the gateway told someone something untrue

Part one is the breaker; part two, written after part one landed and before
its own code, is three more that turned up beside it.

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
