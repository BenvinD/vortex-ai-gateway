## Day 05 — streaming that can be billed, and hanging up
Designed: [day-05 design note](../design/day-05.md)

Built:
- `streaming.py` — `metered()` asks every provider for usage whatever the caller
  requested; `StreamRecord` accumulates the bill as chunks pass through and
  writes exactly one `stream finished` line per streamed request, whatever the
  outcome. The caller's `include_usage` now decides only whether the usage
  chunk is *forwarded* (ADR-018).
- `_stream_events` names `CancelledError`/`GeneratorExit`, records the stream as
  `abandoned` and re-raises, so the teardown carries on into the adapter's
  `finally` and closes the upstream connection (ADR-019).
- The record names the adapter that served the request, not the seam the app
  mounted, and carries the vendor's own model ID — the two fields an invoice is
  reconciled against.
- `examples/stream.sh`: `-N` curl, tokens rendered as they land, and
  `--abandon N` to hang up mid-stream on purpose.

Broke: predicted that killing the client would leave the upstream generating —
nothing in the relay closed the response, so the gateway should have paid for
every token of an answer nobody would read. Ran it against an upstream that
narrates every token it generates, killed the client at 2s of a 15s generation,
and it was **already correct**: the upstream saw the disconnect immediately and
stopped at 8 tokens instead of 60.

| client killed at | upstream generated | upstream stopped after |
|---|---|---|
| 2.0s of a 15s stream | 8 of 60 tokens | 0.0s (next send) |
| 1.0s of a 3s stream | 6 of 12 tokens | 0.0s (next send) |

Observed: the teardown works by accident of two things lining up. `CancelledError`
is a `BaseException`, so the relay's `except Exception` never caught it; and
uvicorn advertises ASGI spec_version **2.3**, which is what makes Starlette's
`StreamingResponse` run its `listen_for_disconnect` task at all. On a server
advertising 2.4 that listener is skipped entirely and disconnects surface as
`send()` raising instead — at which point the relay's generator is abandoned
rather than closed, and the upstream socket waits for the garbage collector.

Learned: "it works" and "it is guaranteed to work" are different claims, and
the gap between them was one dependency's version number. Widening
`except Exception` to `except BaseException` — an edit that looks like tidying —
would have silently turned every abandoned stream back into a fully-billed one.
So the cancellation is now caught by name, recorded, and re-raised, with an
explicit `aclose_stream` in the `finally` for the case where the cancellation
arrives while the relay is parked on the *client's* socket rather than the
provider's.

The second half was cheaper to find and more valuable: the access log said
`200, 1999ms` for that abandoned stream, which is indistinguishable from a
stream that finished quickly. Money was being spent with no delivery behind it
and nothing recorded it. Now:

```json
{"event": "stream finished", "outcome": "abandoned", "provider": "openai",
 "model": "fake-model", "chunks": 6, "total_tokens": null, "duration_ms": 1255.95}
```

`total_tokens` is `null` there, and honestly so: most providers report usage
only in a final chunk that an abandoned stream never reaches. Anthropic is the
exception — its counts arrive in `message_delta`, before `message_stop` — so
abandoned Anthropic streams *do* carry a partial bill and abandoned OpenAI ones
do not. Day 6's cost accounting has to treat a missing usage object as "unknown
but non-zero" rather than as free.

Also learned, twice, from the harness rather than the gateway: a background job
in a non-interactive shell has `SIGINT` ignored, so the first "kill the client"
run killed nothing and produced a clean 60-token success that looked like proof
of a bug. And the demo script rendered a `404` as total silence, because a
rejected request answers with a plain JSON envelope and every line that is not
`data:` was being dropped. Both are now handled in `examples/stream.sh`.
