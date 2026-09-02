## Day 04 — provider routing + the failure taxonomy
Designed: [day-04 design note](../design/day-04.md)

Built:
- `VORTEX_MODEL_ROUTES` — an ordered `pattern=provider` glob table, parsed at
  boot. `ProviderRouter` is itself a `ChatProvider`, so the app mounts a router
  exactly where it mounted one adapter.
- The table doubles as the enable list: only the providers it names get built, a
  named provider with no key fails at startup, and an unmatched model is a `404`
  rather than a silent fallback.
- `ProviderTimeout` / `ProviderRateLimited` / `ProviderBadRequest` /
  `ProviderUnavailable` (+ `ProviderAuthError`, `ProviderProtocolError`), each
  with `retryable`, classified once in `HttpChatAdapter` and mapped to HTTP in
  `routes.py`. `Retry-After` is parsed and forwarded.
- respx tests for all three adapters: the full status matrix, the transport
  failures, and the endpoint URL each adapter must post to.

Broke: predicted that pointing the OpenAI adapter at the black hole
`10.255.255.1:8080` would raise `httpx.ConnectTimeout` → `ProviderTimeout`, and
it did. What I had not predicted was the *bill*:

| failure | httpx raises | becomes | after |
|---|---|---|---|
| black hole, one scalar 30s timeout | `ConnectTimeout` | `ProviderTimeout` | **30.03s** |
| black hole, `connect=5s` | `ConnectTimeout` | `ProviderTimeout` | 5.01s |
| refused (`127.0.0.1:1`) | `ConnectError` | `ProviderUnavailable` | 0.02s |
| connected but mute, `read=2s` | `ReadTimeout` | `ProviderTimeout` | 2.01s |

Observed: `httpx.AsyncClient(timeout=30.0)` sets *all four* phases — connect,
read, write, pool — to 30 seconds. So an unreachable host consumed the entire
generation budget while producing nothing, and did it identically to a slow but
working model. A retry on top of that would have made one dead route cost 60s,
then 90s.

Learned: the connect timeout and the read timeout answer different questions
and must not share a number. A TCP handshake that has not completed in ~5s is
not going to; a model that has not finished generating in 5s very well might.
Split them (`connect_timeout_seconds`, default 5) and an unreachable provider
fails six times faster while a slow one is left alone. The other half of the
lesson is that *refused* is not *unreachable*: it comes back in 20ms and is a
different error class, so a retry loop should treat "nobody is listening" very
differently from "nobody answered" — which is exactly what the taxonomy now
lets it do.

Quiz misses: httpx timeout phases (connect/read/write/pool) and what a bare
`timeout=` sets; `Retry-After` may be an HTTP date, not just seconds.

One paragraph I could say in an interview: A gateway's error handling is only
as good as its vocabulary. We classify every provider failure once, at the HTTP
boundary, into six types carrying a `retryable` flag — so the retry policy, the
circuit breaker and the HTTP status mapping all branch on the same judgement
instead of each re-deriving it from a status code. Getting that boundary right
also exposed a timeout bug we would otherwise have shipped: a single httpx
`timeout=30` applies to connect *and* read, so a black-holed upstream held a
worker for thirty seconds before failing. Splitting the connect budget out
dropped that to five, and the measurement is in the repo's day notes.
