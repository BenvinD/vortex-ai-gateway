## Day 14 — places the gateway told someone something untrue
Designed: [day-14 design note](../design/day-14.md)

### Part one — the half-open trial that never reported back

Built:
- `resilience.py` — `allow()` returns a `Permit | None` instead of a `bool`.
  The permit says whether its call is the half-open trial, and the breaker
  holds the trial's permit by identity (`eq=False`), so `release_trial(permit)`
  is a no-op for anyone but the holder: a call admitted while closed, or last
  window's trial (ADR-030).
- `providers/resilience_wrapper.py` — the trial is one call: a retryable
  failure records the verdict, gives up under a new fourth reason,
  `half-open trial failed`, and re-raises the real error. Every attempt hands a
  trial slot back in a `finally`, which covers the three endings that learned
  nothing — a non-retryable failure, a cancellation, a non-provider exception —
  and is a no-op after a verdict. The cancellation branch lost its own release;
  the `finally` does it now.
- Tests: one parametrised test over all five ways a trial can end, one for the
  still-down trial's call count and escalated window, three for ownership
  (including the concurrent cross-release through the wrapper), and one for
  the new give-up reason. 745 passing; ruff, format and mypy clean.
- ADR-030, its index row, and the matching sentences in `AGENTS.md` and
  `docs/architecture.md`. `.run/` and `debug/` are now gitignored.

The repro, against the code as it was and as it is (`max_attempts=3`, open
window 10 s, a healthy upstream offered a simulated day later):

| Trial ending | Before: caller saw | Before: breaker | After: caller saw | After: breaker |
|---|---|---|---|---|
| still down | `CircuitOpenError`, no `Retry-After` | half-open, slot held — **wedged** | `ProviderUnavailable` | open, `retry_after=20.0` |
| caller's 400 | `ProviderBadRequest` | half-open, slot held — **wedged** | `ProviderBadRequest` | half-open, slot free |
| adapter bug | `KeyError` | half-open, slot held — **wedged** | `KeyError` | half-open, slot free |
| still down, `max_attempts=1` | `ProviderUnavailable` | open, `retry_after=20.0` | same | same |
| closed-era call cancelled mid-trial | — | third caller admitted: `True` | — | third caller admitted: `False` |

**Broke: predicted that path A's leak and path A's retrying were the same bug;
observed that they come apart, and that the fix for B and C quietly "fixes" A
the wrong way.** The design note's prediction matched the repro row for row,
which on its own taught nothing. The gap came from re-breaking the fix one
piece at a time to check each test could fail:

| Fix removed | Tests that failed |
|---|---|
| the `finally` release | `[callers-400]`, `[adapter-bug]`, `[cancelled]` |
| the `if permit.trial:` branch | `[still-down]`, `…reopens_without_retrying`, `…gives_up_under_a_reason_of_its_own` |
| the owner check in `release_trial` | the three ownership tests |

The second row is the interesting one. With the `finally` in place and the
one-call rule removed, the trial does *not* wedge. Its first attempt fails, the
`finally` hands the slot back before the backoff, and its retry calls `allow()`,
finds the slot free, and takes a *new* trial permit. Three upstream calls, then
`record_failure`, then open — a breaker that recovers, from the outside
indistinguishable from fixed. It is alternative (2) from the design note, the
one rejected because the slot sits free during every backoff and a second
caller can take it as a concurrent trial. The design note said "three calls
would mean the trial is still retrying", assuming retrying meant being refused
by its own reservation. It doesn't have to. Any test asserting only "the breaker
recovers" passes against the rejected design; `assert inner.calls == 1` is the
line that tells them apart.

The original bug had the same shape one level up. The suite already had a
test named for a failed trial reopening the breaker for longer, and it passed:
it calls `allow()` then `record_failure()` on the breaker directly, which is
the sequence the wrapper was *supposed* to produce. With `max_attempts > 1` the
wrapper never produced it. It produced `allow()`, a failure, a backoff, and a
second `allow()` refused by its own slot. The breaker's contract was tested.
Its only caller was never tested against it, apart from the cancellation case
that the Day 6 fix added.

**Broke, tooling.** Adding `ResilientProvider` to `test_metrics.py`'s imports
before writing the test that uses it got the import deleted. The PostToolUse
hook runs `ruff check --fix` on every edit, F401 removed the unused name, and
the next run failed with `NameError`. With an autofixing hook, an import goes
in the same edit as its first use, or after it.

Found, measured, and left for part two, which fixes it: a give-up on budget or
on deadline is counted twice. `_should_retry` counts its own reason, returns
`False`, and the code after the loop counts `attempts exhausted` as well, and
logs a second give-up line claiming `attempts=3` after one call:

```
budget  : {'attempts exhausted': 1.0, 'retry budget exhausted': 1.0}
deadline: {'attempts exhausted': 1.0, 'deadline reached': 1.0}
```

`sum by (reason)` on the Grafana panel therefore double-counts exactly the two
give-ups that mean the outage is wider than one request. The existing test
asserts its own reason is 1 and never that the others are 0, so it passes. The
new trial path does not do this, and its test asserts `attempts exhausted`
stays at 0.

Learned: when a fix lands as several parts, delete each part in turn and watch
which tests go red. A test that stays green with one part removed is testing
what the parts do together, and the parts can produce the same symptom through
a design you rejected. And a component tested only by calling it in the order
its caller is *meant* to call it has a contract with no caller under test.

**One paragraph I could say in an interview.** My gateway's circuit breaker
lets one trial request through after the open window, and that slot has to be
handed back or the breaker refuses everything forever, with no timer to rescue
it. A Day 6 fix handled the client hanging up. I found three more exits that
leaked it, and the worst was the ordinary one: the provider is still down at
the first probe, the retry loop retries, and the trial's own retry is refused
by its own reservation, so every outage longer than one window disabled that
provider until restart. The fix made the trial a single call, released the slot
in a `finally` for the endings that learned nothing, and made ownership
explicit — `allow()` returns a permit compared by identity, so a long request
from before the outage can't free the current trial's slot. What I'd want to be
judged on is checking the fix by taking it apart. With the one-call rule
removed, the `finally` alone made the breaker recover anyway, by releasing
between attempts, which was the design I had rejected. Only the assertion on
the upstream call count could tell the two apart.

### Part two — two refusals that lie, and a counter that counts twice

Built:
- `routes.py` — the route calls `provider.stream()` itself, before the
  `StreamingResponse` exists, so the router's synchronous `UnroutableModelError`
  lands before the `200`. Both paths now fail through one helper, `_failed`,
  which logs, releases the reservation in full and renders the envelope.
  `_stream_events` takes the stream it is given (ADR-032).
- `metering.py`, `error_handling.py` — `Meter.admit` refuses an estimate above
  the key's whole TPM with `RequestTooLargeError`: a `400`,
  `invalid_request_error`, code `request_exceeds_token_limit`, no
  `Retry-After`. It is decided from the key before Redis is asked (ADR-031).
- `providers/resilience_wrapper.py` — `_should_retry` became
  `_backoff_or_give_up`, which returns the give-up reason instead of reporting
  it, and `_report_give_up` writes the one line and the one count, with the
  attempts actually made.
- Tests: the oversized test inverted from `429` to `400` and now checks the
  refusal spent no request; an exactly-at-capacity/one-over pair; an
  unroutable-model test over both shapes, buffered and streamed, with metering
  live; and the give-up test now asserts every *other* reason reads 0 and that
  there is one log line. 749 passing; ruff, format and mypy clean.
- ADR-031, ADR-032, their index rows, and the matching sentences in `AGENTS.md`
  and `docs/architecture.md`.

Before and after, measured:

| Case | Before | After |
|---|---|---|
| unroutable model, streamed, three times (`tpm=10_000`, 1,001 reserved each) | `200`, empty body; tokens remaining 9000 → 8001 → 7001, and 6002 on the next request | `404` `model_not_found`; 9000 every time |
| estimate 1,500 on a `tpm=1_000` key | `429`, `Retry-After: 30`, and the same again after waiting an hour, three times | `400` `request_exceeds_token_limit`, no `Retry-After` |
| a `--tpm 500` key, no `max_completion_tokens` (1 + 512) | `429`, `Retry-After: 2`, forever | `400`, message names 513 and 500 |
| the same key at exactly 500 | — | `200`, `requests_remaining=99`: the refusal before it spent nothing |
| a budget give-up | `{'attempts exhausted': 1.0, 'retry budget exhausted': 1.0}`, two lines, the second claiming `attempts=3` | `{'retry budget exhausted': 1.0}`, one line, `attempts=1` |
| a deadline give-up | `{'attempts exhausted': 1.0, 'deadline reached': 1.0}` | `{'deadline reached': 1.0}` |

**Broke: predicted all three rows of the design note's experiment, and got
them; the gap was in the tests that had been passing all along.** Every one of
the four bugs today already had a test, and every one of those tests asserted
something true:

| Bug | The test that passed | What it asserted | What it never asserted |
|---|---|---|---|
| breaker wedge | `test_a_failed_trial_reopens_the_breaker_for_longer` | the breaker reopens when called in the right order | that the wrapper calls it in that order |
| unroutable stream | `test_an_unroutable_stream_fails_before_it_is_iterated`, plus the buffered `404` test | the router raises before iteration; the buffered path is a `404` | the streamed path through HTTP, where the route defeated the router |
| oversized request | `test_a_request_larger_than_the_whole_token_allowance_is_refused` | it is refused, and the refusal spent no request | that the `Retry-After` it sent could ever come true |
| give-up double count | `test_the_three_ways_to_give_up_stay_distinguishable` | the right reason reads 1 | that the wrong one does not read 1 as well |

The unroutable stream is the cleanest example. The router's own test named the
danger in its docstring — "a failure after the `200` cannot carry a status" —
and protected against it at its own layer, and `routes.py` called `stream()`
one layer up, inside the body generator, which put the call after the `200`
anyway. Re-breaking the fix confirmed the shape: with `routes.py` put back as
committed, `[streamed]` failed and `[buffered]` passed. That is
`tests/CLAUDE.md`'s second failure class exactly — each axis tested, the
combination never — and it has now appeared twice in one day, once in each
part.

**Broke, tooling, again.** Part one's note records that the PostToolUse hook's
`ruff check --fix` deletes an import added before its first use. Later the
same afternoon it deleted `RequestTooLargeError` from `metering.py` for the
same reason, caught by reading the import line back before any test ran. Writing a lesson down did not change
the order the edits were made in. Making the ordering a habit did: the tests
after it went in body first, imports second, and nothing was deleted.

Learned: a test of a refusal should assert the whole of what the refusal tells
its reader — the status *and* the advice it carries, the reason that was
counted *and* the ones that were not. Every test above checked the half of the
claim the author was thinking about, and each bug lived in the other half.

**One paragraph I could say in an interview.** After fixing the circuit
breaker, I went after three smaller lies the gateway told. A streamed request
for an unknown model got `200` with an empty body and kept its token
reservation, because the route asked for the stream inside the response body,
after the status was sent, which defeated a router built specifically to fail
early. A request bigger than a key's whole tokens-per-minute allowance got a
`429` with a `Retry-After` that could never come true, since a token bucket
never holds more than its capacity; it is now a `400` that says what to shrink,
decided before Redis is asked. And a give-up was counted under two reasons at
once. What tied them together was that each bug had a passing test, and each
test asserted something true but incomplete: that a refusal happened, not that
its advice worked; that the right counter moved, not that the wrong one
didn't. So I now write refusal tests against everything the response claims,
and I check each fix by putting the old code back to see which test goes red.

### Part three — a stream that waits for its first chunk, and a key that warns

Built:
- `providers/resilience_wrapper.py` — both retry loops are generic over what
  one attempt *is*: `ResilientProvider._call` and
  `FallbackProvider._first_to_serve` take an awaitable factory, `complete()`
  passes "await the completion", and `stream()` passes `_open`, which opens the
  stream and awaits its first chunk. Each wrapper's `stream()` is now a
  generator that yields that chunk, relays the rest untouched, and closes the
  inner stream in a `finally`. The breaker's verdict on a stream is its first
  chunk (ADR-032).
- `routes.py` — the route awaits the first chunk inside a new `provider.stream`
  span before it builds the `StreamingResponse`. A failure there is a status
  through `_failed`, with a `failed` stream record. A cancellation there is
  recorded as `abandoned` and settled as one. `_settle_stream` lost its
  "failed with no chunks" branch, because nothing that reaches it can be that
  any more.
- `cli.py` — `vortex-keys create` warns on stderr when the key's effective TPM,
  meaning the flag or the default it inherits, is no more than the assumed
  completion. The token is still the only thing on stdout.
- Tests: scripted streams for retry-until-first-chunk, the breaker tripping on
  streams that never start, no retry after the first chunk, and hop-before
  versus never-after; HTTP-level `502`-then-`503` and retry-to-`200`; a
  pre-first-chunk failure with metering live; a cancellation during the wait;
  a hang-up through both wrappers; the wrappers' `finally`; six cases for the
  key warning. The old in-band error test now fails *after* a chunk, which is
  the only place an in-band error can still happen.
- ADR-032 rewritten in place, as decided, so part two's references to it
  describe its first version. ADR-031's consequences, dated updates on ADR-001
  and ADR-020, three index rows, `AGENTS.md` and `docs/architecture.md`.

The planned experiment, through the real route, router, `ResilientProvider` and
OpenAI adapter against a respx upstream, run on the committed code (a detached
worktree of `HEAD` on `PYTHONPATH`) and on the working tree:

| Upstream | Before | After |
|---|---|---|
| `503`, `503`, then a stream | `200`, 1 upstream call, an error event, no text | `200`, 3 calls, `'hello'`, no error |
| `503` every time | `200`, 1 call, an error event | `502`, 3 calls, the error as an envelope |
| the next request | `200`, called the dead upstream again | `503`, `Retry-After: 59`, no call |
| fails after its first chunk (doubles) | — | `200`, 1 call, the error as the final event |

```
$ vortex-keys --db … create --name tiny --tpm 500
Store this token now. Only its hash is kept, so it cannot be shown again.
Warning: --tpm 500 is no more than the 512 tokens assumed for a completion when
a request sets no max_completion_tokens (…), so nearly every such request will
be refused with a 400. Raise the limit, or have callers set max_completion_tokens.
vtx_efba90da4795_…
```

**Broke: predicted the experiment, and got it; then a test written to prove one
guarantee turned out to prove nothing about it.** Re-breaking the fix one piece
at a time:

| Fix removed | Tests that failed |
|---|---|
| the route awaiting the first chunk | 4: the pre-first-chunk status, `502`-then-`503`, the metered release, the cancellation record |
| `ResilientProvider.stream` using the shared loop | 4: both retry tests, the breaker test, `502`-then-`503` |
| the `abandoned` record on a cancelled wait | 1 |
| the wrappers' `finally` closing the inner stream | **0** |

`test_a_hang_up_reaches_the_adapter_through_the_resilience_wrappers` was
written for the fourth row, and passed with that `finally` deleted. In that
test the cancellation lands inside the adapter, which is awaiting its
upstream, so the adapter unwinds itself and the wrapper is never asked to close
anything. The `finally` matters only when the relay is suspended at a `yield`.
Even then, removing it does not leak for long: asyncio's async-generator
finalizer closes the abandoned adapter a loop turn later, so no end-to-end
test can tell the difference. The fix is a unit test that closes the wrapped
stream at a `yield` and asserts in the same instant `aclose()` returns. It
failed for both wrappers with the `finally` gone. The hang-up test keeps a
docstring that now says what it does *not* prove.

**Broke, in part two's test, found in part three.** Running
`test_resilience.py` before `test_metrics.py` failed the give-up test from part
two: `capture_logs()` saw no lines. The cause has two halves. `gateway.py` runs
`app = create_app()` at import, which configures structlog with
`cache_logger_on_first_use=True` before the first test starts. So whichever
test first logs through `resilience_wrapper` caches that logger for the rest of
the session, and `capture_logs()` cannot see a cached logger. The suite's
alphabetical order runs `test_metrics.py` first, which is the only reason it
passed. The test now swaps in its own `LogCapture` logger, and both orders
pass. The import-time configuration is left as it is and written down here: it
is the same kind of leak `conftest.py`'s `_reset_structlog` exists for, from
before the first test, where that fixture cannot reach.

Learned: when the runtime eventually provides a guarantee itself, as the
garbage collector does by closing a dropped generator, an end-to-end test
cannot tell code that provides it from code that leaves it to the runtime.
Only an assertion at the moment the guarantee is owed can. And a test that
touches global state has not been shown to pass until it has passed out of
suite order.

**One paragraph I could say in an interview.** Streams were the gap in my
gateway's resilience. The route committed the `200` as soon as it had a
generator, but the adapter makes its HTTP request only when that generator is
first iterated. So every upstream `5xx` became a `200` with an error event, and
the retry loop, the breaker and the fallback chain never saw a stream at all. I
moved the commit point to the first chunk and made both resilience loops
generic over what one attempt is: a completion, or "open the stream and await
its first chunk". Streams got exactly the retries, breaker verdicts and hops
completions have, in the one window where retrying can't splice two
generations together. Against a dead upstream that turned "`200` with an
error, every time" into a `502` and then a `503` with `Retry-After`. The part
I'd want to be judged on is what deliberately breaking it showed. The test I'd
written to prove a hang-up reaches the adapter through the wrappers passed with
the relevant code deleted, because asyncio's generator finalizer closes the
adapter a moment later anyway. Only a unit test asserting at the instant of
`aclose()` could tell my code from the runtime's safety net.
