---
name: add-tests
description: Write tests that match this repo's conventions — the real-dependency doubles, the src/-layout import rule, the token-cheap iteration loop, and the two failure classes this suite has actually been caught by. Use when adding or extending tests, when asked to "add test cases", "cover this", "write tests for X", or when a change lands without tests.
---

# add-tests — the conventions, and the two bugs this suite has missed

Argument (optional): what to test. Otherwise infer from the current change.

## Before writing: pick the right double

This repo has a strong opinion here. A hand-written double that provides the
guarantee under test proves nothing about the thing under test.

| Testing | Use | Why |
|---|---|---|
| the HTTP surface, routing, metering seam | `MockProvider` + `CannedReply` / `ScriptedOutcome` | The scripted `ChatProvider` (ADR-011). Assert on `provider.received_requests` — *what the gateway decided to send* is usually the real assertion, not just what came back. |
| a vendor adapter | `respx` against the real `httpx` client | Exercises the actual transport and the actual status→exception mapping. |
| the rate limiter | `fakeredis[lua]` | The `lua` extra pulls in lupa, so the limiter's **real Lua script** executes. A Python re-implementation of a token bucket provides atomicity for free and proves nothing about the script that has to provide it. |
| the ledger / spend | `fakeredis.aioredis.FakeRedis` | See `tests/test_spend.py`. |
| the contracts | the golden files under `tests/golden/` | They assert nothing in the file is dropped or altered by a round trip — the check that `contracts/` describes the real wire format rather than our memory of it. |

## Mechanics

- **`asyncio_mode = "auto"`** is set. Async tests need no
  `@pytest.mark.asyncio`. Adding one is noise.
- **Never add `pythonpath` to `pyproject.toml`,** and never `sys.path.insert`.
  Tests import the *installed* distribution on purpose; putting `src/` on the
  path lets a broken wheel pass the suite. If an import fails after adding a
  module, the fix is `uv sync`, not a path hack.
- **Build the app per test** with `create_app(settings=Settings(_env_file=None), provider=...)`.
  `_env_file=None` is what isolates the test from the developer's `.env`; omit it
  and the suite passes or fails depending on whose laptop it is on. Never import
  the module-level `app` — that exists only as the uvicorn entrypoint.
- **structlog** is reset between tests by the autouse `_reset_structlog` fixture
  in `conftest.py`. Assert on log records rather than on stdout.
- Docstring every test with the behaviour it pins, in a sentence. Match the
  existing style: `"""A buffered completion comes back in the shape an OpenAI
  client parses."""` — the behaviour, not the mechanics.

## Iterate cheaply, verify once

`addopts` carries `--cov=vortex_ai_gateway --cov-report=term-missing`, so a bare
`uv run pytest` prints the whole per-file coverage table every time. While
writing:

```bash
uv run --no-sync pytest -q --no-cov -x tests/test_<file>.py
uv run --no-sync pytest -q --no-cov -x tests/test_<file>.py::test_<one>
```

Run the coverage-bearing suite once at the end, via `/verify`.

## The three failure classes this suite has actually been caught by

The first two are in `docs/notes/day-07.md`; the third was caught by CI in
September 2026. All three passed locally first.

**1. A test that samples randomness once is a coin flip, not a test.**
`parse_key_id` failed on 48.3% of minted keys. A test minting *one* key failed
about half its runs — which in a CI log reads as flakiness and gets re-run rather
than read. The test minting fifty and asserting fifty distinct IDs failed with
probability 1 − 4.7e-15.

> When a test loops over anything random, loop far enough to turn a coin flip
> into a certainty.

So: if the thing under test involves `secrets`, a hash, jitter, or an ID, assert
over tens or hundreds of samples, not one. Compute the escape probability and
make it negligible.

**2. Test the *cross product* of endings and subsystems, not each axis alone.**
An abandoned stream settled into nothing: the ledger row was never written,
while `request settled` was still logged, so the logs claimed success. It
survived because *every* abandonment test ran with a pass-through meter and
*every* metered streaming test completed normally. **Abandoned and metered was
never combined** — and that is precisely the request the accounting exists for.

A stream has three endings — **completed, failed, abandoned** — and they are not
interchangeable: an abandoned stream runs its `finally` inside a scope Starlette
has already cancelled, where the next `await` that yields raises
`CancelledError` immediately. When adding a test to any subsystem a stream
touches, ask which of the three endings it runs under, and whether the other two
are covered *with that subsystem live*. Prefer a parametrised test over the
endings to three separate ones that quietly share a default.

**3. A test that reads the wall clock is a test that fails on a busy runner.**
The token bucket refills by `(now - seen) * rate`, so at `tpm=10_000` it gains
~167 tokens a second. `test_a_finished_request_refunds_what_it_did_not_use`
asserted an exact bucket movement of 5 against the real clock: it passed on a
laptop and failed in CI with `assert (8999 - 8995) == (10 // 2)` — the refund
looked one token short because a few extra milliseconds had refilled one token.

`RateLimiter` and `SpendLedger` both take `clock=` for exactly this reason, and
`ratelimit.py` says so outright: a limiter "that reads the clock itself is
non-deterministic, and it cannot be driven by a test". `tests/test_spend.py`
pins the ledger's clock and `tests/test_resilience.py` has a fake clock so it
"never flakes on a loaded machine" — so when a test asserts on anything
time-derived, pin the clock rather than assuming your machine is fast enough.

The general form of all three: the bug lives where a test's *environment*
varies — the sample it drew, the combination it happened to exercise, the speed
of the machine it ran on. Pin what varies.

## Before finishing

```bash
uv run --no-sync pytest -q --no-cov            # whole suite, no coverage table
```

Then `/verify`. If the new test found a real bug, it belongs in
`docs/notes/day-NN.md` under "Broke" — that is what `/day-note` is for.
