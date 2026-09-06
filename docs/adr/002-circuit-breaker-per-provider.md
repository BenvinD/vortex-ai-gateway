# ADR-002: One circuit breaker per provider, with a single half-open trial
Date / Status: 2026-09-06 / accepted
Context: Retrying does not help a provider that is *down*; it makes it worse and
pins a worker for `attempts × timeout` per request while returning nothing. The
gateway needs to stop calling and start failing fast — and then find out when to
start again without sending the whole herd back at once.
Options: A) retries only B) one breaker for the gateway C) one breaker per
provider, three states, one half-open trial, escalating open window.
Decision: C. Per-provider is the load-bearing part: a shared breaker would let a
dead OpenAI refuse Anthropic traffic, which is the opposite of what a
multi-provider gateway is for. Only failures that survive the retry loop count,
and only ones that mean the *provider* is unhealthy — a `ProviderBadRequest`
or an `UnsupportedParameterError` never moves it, or one client sending garbage
could take a provider offline for every other client. Failures are counted per
**request**, not per attempt, so the threshold means what it says. A `429` does
count today, which is the decision most likely to be wrong: throttling means
"you are sending too much", not "I am broken", and the honest fix is a
concurrency limiter rather than a breaker — revisit when one exists. Refusal
raises `CircuitOpenError`, a `503` carrying `Retry-After` from the breaker's own
clock, distinct from `ProviderUnavailable`'s `502` because "we declined to ask"
is not "the upstream answered badly".
Consequences: An outage costs one refused call rather than three timeouts, and
recovery is probed by exactly one request. State is per worker process, so with
N uvicorn workers the fleet needs N×threshold failures to protect itself and
runs N probe schedules. Acceptable while the fleet is small; the rejected option
wins once it is not, and then the state belongs in Redis — with its own timeout
and a fail-open policy, or the thing protecting us becomes the thing that takes
us down.
