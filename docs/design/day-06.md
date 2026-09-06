## Day 06 — surviving a provider that is having a bad day

**Building.** Every upstream call is currently one attempt: a blip is a 502 to
the caller, and a provider that is down is a 502 *per request*, each one paying
the full 30-second timeout before failing. Three gaps, in order of what they
cost: transient failures are not retried; a dead provider keeps being called;
and when it is down there is nowhere else for the traffic to go.

**Alternatives — retries.** (1) Retry everything a fixed number of times:
trivial, and it retries the caller's malformed request three times for money.
(2) Retry on HTTP status: works, but re-derives a classification `errors.py`
already owns, and has no answer for failures that never got a status.
(3) Branch on the taxonomy's `retryable` attribute, with full jitter and a
wall-clock deadline. Chose (3) — see ADR-001. The attribute exists precisely
so retry, breaker and status mapping cannot drift apart.

**Alternatives — repeated failure.** (1) Rely on retries alone: an outage then
costs `attempts × timeout` per request and pins workers doing nothing.
(2) Fail every call for a fixed cooldown after N failures: cheap, but recovery
is a cliff — the whole herd returns at once. (3) Three-state breaker with a
single half-open trial and an escalating open window. Chose (3) — see ADR-002.
One breaker per provider, because a dead OpenAI must not stop Anthropic.

**Alternatives — nowhere to go.** (1) Nothing: a 503 is honest, and the gateway's
whole promise was that it manages this. (2) Automatic failover on any error:
silently changes which model answers, and hops requests that would fail
everywhere. (3) Explicit, config-driven chains that hop only when *this*
provider cannot serve the call. Chose (3) — see ADR-020.

**Planned experiment.** Script a provider that fails, count upstream calls
across three requests, and check the third makes none — the breaker should be
open by then and the hop instant. Prediction: calls go 3, 3, 0. The number to
watch is the second request; if it also makes three calls, failures are being
counted per attempt rather than per request and the threshold means a third of
what it says.
