# tests/ — conventions and the two bugs this suite has missed

Loaded when work happens in this directory.

## Imports and app construction

- **Never** add `pythonpath` to `pyproject.toml` and never `sys.path.insert`.
  Tests import the *installed* distribution on purpose; putting `src/` on the
  path would let a broken wheel pass the run. An `ImportError` after adding a
  module means run `uv sync`, not patch the path.
- Build the app per test: `create_app(settings=Settings(_env_file=None), provider=...)`.
  `_env_file=None` is what isolates the test from the developer's `.env` — omit
  it and the suite's result depends on whose laptop it runs on. Never import the
  module-level `app`; it exists only as the uvicorn entrypoint.
- `asyncio_mode = "auto"` is set — async tests need no `@pytest.mark.asyncio`.
- `conftest.py`'s autouse `_reset_structlog` stops structlog's global config
  leaking between tests. Assert on records, not stdout.

## Use the real dependency, not a double that grants the guarantee

- **Rate limiter → `fakeredis[lua]`.** The `lua` extra pulls in lupa so the
  limiter's *real* Lua script executes. A Python re-implementation of a token
  bucket provides atomicity for free and proves nothing about the script that
  has to provide it.
- **Adapters → `respx`,** against the real `httpx` client, so the actual
  status→exception mapping is exercised.
- **HTTP surface / routing / metering → `MockProvider` + `CannedReply`.** Assert
  on `provider.received_requests`: what the gateway decided to *send* is usually
  the real assertion.
- **Contracts → the golden files.** They assert nothing in the file is dropped
  or altered by a round trip. A capture that fails to parse is the contract being
  out of date — fix `contracts/`, never the fixture.

## Iterate without burning context

`addopts` carries `--cov-report=term-missing`, so every plain `pytest` prints
the whole coverage table. While working:

```bash
uv run --no-sync pytest -q --no-cov -x tests/test_<file>.py
```

Coverage runs once at the end, via `/verify`.

## The three failure classes that got through 541 passing tests

The first two from `docs/notes/day-07.md`; the third from a CI-only failure.

**A test that samples randomness once is a coin flip.** `parse_key_id` failed on
48.3% of minted keys; the test minting *one* key failed half its runs and read
as CI flakiness. The test minting fifty failed with probability 1 − 4.7e-15.
Loop far enough to turn a coin flip into a certainty.

**Test the cross product, not each axis.** An abandoned stream wrote no ledger
row while still logging `request settled`. It survived because every
abandonment test used a pass-through meter and every metered streaming test
completed normally — *abandoned and metered was never combined*, and that is the
one request the accounting exists for. A stream has three endings — completed,
failed, **abandoned** — and the abandoned one runs its `finally` inside an
already-cancelled scope where the next yielding `await` raises `CancelledError`
immediately. When testing anything a stream touches, cover all three endings
with that subsystem live; parametrise over them rather than sharing a default.

**A test that reads the wall clock fails on a busy runner.** The token bucket
refills by `(now - seen) * rate` — ~167 tokens a second at `tpm=10_000` — so an
exact assertion on bucket movement passed locally and failed in CI by one token.
`RateLimiter` and `SpendLedger` both take `clock=` precisely so a test can pin
it; `ratelimit.py` says a limiter that reads the clock itself "cannot be driven
by a test". Pin the clock whenever an assertion is time-derived.

Use the `add-tests` skill for the full checklist.
