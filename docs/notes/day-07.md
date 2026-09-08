## Day 07 — keys that can be revoked, limits that hold, and a bill
Designed: [day-07 design note](../design/day-07.md)

Built:
- `keys.py` + `vortex-keys` — SHA-256-hashed keys in SQLite as
  `vtx_<key_id>_<secret>`, looked up by the public ID and confirmed with
  `hmac.compare_digest`, so a missing row and a wrong secret take the same path
  (ADR-003). `auth.require_api_key` now returns a `Principal` rather than a
  token: `key_id` is what a limit counts against and a bill attaches to, and it
  must not be the secret, because it ends up in Redis keys and log lines.
- `ratelimit.py` — per-key RPM and TPM as one token bucket pair, checked and
  debited in a single Lua script, `now` passed in from Python. Reserve an
  estimate, reconcile against real usage on settlement, clamp the refund at
  capacity (ADR-021).
- `pricing.py` + `spend.py` + `GET /v1/usage` — token counts in Redis,
  `Decimal` prices applied at read time, `None` for a model the table cannot
  price and a named list of which ones (ADR-022).
- `metering.py` — the seam `routes.py` actually calls: `admit` before, `settle`
  or `discard` after. Three endings, three behaviours, which is the part that
  took the longest to get right — see the second "broke" below.

**Broke: predicted that a well-formed token would always parse, and roughly half
of them did not.** `parse_key_id` split the token on `_` and demanded three
parts. `secrets.token_urlsafe` emits base64url — whose alphabet *includes* the
underscore — so any secret that happened to contain one produced four parts and
a token that failed to authenticate. The failure rate is the punchline:

| secret contains `_` | share of minted keys | symptom |
|---|---|---|
| yes | 48.3% (measured over 200k mints) | 401 on every request, forever |
| no  | the rest | works perfectly |

Observed: the tests that mint *one* key are a coin flip — they fail about half
the runs, which in a CI log looks like flakiness and gets re-run rather than
read. The test that mints fifty and asserts fifty distinct IDs fails with
probability 1 − 4.7e-15, which is to say always. That gap is the whole lesson:
the bug was not hard to catch, it was hard to catch *reliably*, and a single
sample of a 50% failure is indistinguishable from bad luck. When a test loops,
make it loop far enough to turn a coin flip into a certainty.

The narrower lesson is about the format: a delimiter belongs to the half with
the *smaller* alphabet. The key ID is hex and could never contain `_`; the
secret is base64url and half the time does. The fix is `split("_", 2)` — split
twice, and let the secret keep whatever it contains.

**Broke again, and this one was invisible: an abandoned stream settled into
nothing.** The accounting went in the `finally` of `_stream_events`, next to
`record.log()`, awaited in place — which reads correctly and is correct for two
of the three endings. For the third it is not. An abandoned stream reaches that
`finally` inside a scope Starlette has *already cancelled*, and under a cancelled
scope the next `await` that yields raises `CancelledError` immediately. Redis I/O
yields. So the write never completed.

What made it invisible is the shape of the failure. `request settled` was logged
— that line runs before the Redis call — so the logs said the request had
settled. The provider stream was still torn down properly, so nothing hung and
nothing leaked. Only the ledger was empty, and the reservation stayed held:

| stream ending | `request settled` logged | ledger row written |
|---|---|---|
| completed | yes | yes |
| failed | yes | yes |
| **abandoned** | **yes** | **no** |

Predicted: `total_requests == 1, total_unmetered_requests == 1`.
Observed, before the fix: `0` and `0`.

The abandoned stream is precisely the request this whole path exists to account
for — tokens generated, none counted, ADR-019's entire point — and it was the
one ending that silently wrote nothing. 541 tests did not see it, because every
abandonment test ran with a pass-through meter (nothing to suspend on) and every
metered streaming test completed normally. Abandoned *and* metered was never
combined.

Learned: an `await` in a `finally` is not a neutral act; it is a request for
permission from whoever is cancelling you. Settlement for an abandoned stream is
now spawned as a task rather than awaited, held in a module-level set so the
loop cannot collect it mid-write, and `aclose_stream` moved into a nested
`finally` so it runs whatever the settlement does. The other two endings are
still awaited in place, where nothing is cancelling anything and having the
write land before the response ends is worth having.

Two smaller ones from the same review. `hmac.compare_digest` short-circuits on a
length mismatch, so comparing an unknown key against `""` made the missing-row
path measurably faster than the wrong-secret path — the exact timing signal the
constant-time compare was there to remove. The dummy is now `hash_token("")`,
which is the right length. And a Redis outage emits *two* `failing open`
warnings per metered request, one from `admit` and one from `reconcile`; correct,
but under load it is noisy enough to bury the signal, and it is the first thing
to sample or de-duplicate if this ever pages someone.

Also learned, from the tooling rather than the gateway: `types-redis` was in the
dev group from day one, unused, added pre-emptively. redis-py has shipped
`py.typed` since 5.0, and the stub package still describes the 4.x API — so
mypy was checking a Redis that has not existed for three major versions and
reported `"Redis" has no attribute "aclose"` against a runtime that has had it
all along. Removing the stubs fixed it. `CLAUDE.md` already warns about
pre-emptive `ignore_missing_imports` sections; a pre-emptive *stub package* is
the same mistake with worse symptoms, because it fails by being confidently
wrong rather than by doing nothing.

**Experiment, as predicted.** 200 concurrent admissions against a bucket of 50:
exactly 50 admitted, 150 refused, and the bucket read back at 0.0 rather than
merely near it. Run against `fakeredis[lua]`, which executes the real script
through lupa — a Python re-implementation of the bucket would have provided
atomicity for free and proved nothing.

Then the same thing against a real Redis, 5 rpm, seven requests:

```
1: remaining-requests: 4   remaining-tokens: 1498
5: remaining-requests: 0   remaining-tokens: 1474
6: remaining-requests: 0   remaining-tokens: 1970   retry-after: 12
```

Two things worth reading twice. The token bucket falls by ~6 per request, not by
the 502 each one *reserved* — that is the reconcile working, and it is what makes
TPM a limit on tokens rather than on requests-times-a-guess. And request 6's
`remaining-tokens` is *higher* than request 5's, because by then request 5's
refund had landed.

Finally, killed Redis mid-run and sent a request from a key that was already
over its RPM: `200`, one `rate limiter unavailable; failing open` warning, and
`/readyz` still `200`. That last part is the deliberate one — the limiter
degrades, so draining the instance would convert a degradation into an outage.

**One paragraph I could say in an interview.** Rate limiting a model gateway is
harder than rate limiting an API, because the expensive unit — the token — is
not known until after the work is done. We enforce two allowances per key,
requests and tokens, and both have to fit or neither is spent; that "or neither"
is why the check and the debit live in one Lua script rather than three round
trips, since with three a request the token bucket refuses has already burned a
request. For the tokens themselves we reserve an estimate on admission and
reconcile it against real usage when the request settles, which makes the
estimate's *accuracy* almost irrelevant — it governs how much a caller may have
in flight, not what they are charged. Every response carries its remaining
allowance, not just the rejections, so a well-behaved client can slow down
before it is throttled. And the whole thing fails open: a limiter that takes the
gateway down when it cannot enforce a limit has inverted its own purpose, so
Redis being unreachable produces a warning and an admitted request, and Redis is
deliberately not a readiness check — draining a working instance because the
limiter is degraded turns a degradation into an outage.
