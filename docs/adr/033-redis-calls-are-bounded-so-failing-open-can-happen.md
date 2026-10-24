# ADR-033: Every Redis call is bounded, short, so that failing open can happen
Date / Status: 2026-10-24 / accepted
Context: The limiter, the ledger and both caches fail open on `RedisError`
(ADR-021, ADR-004), but the client was built with no timeouts, and redis-py has
none by default. A Redis that refuses connections raises at once; one that
accepts and never answers — a stalled primary, a full backlog, a black-holed
route — raises nothing, so every request hung behind `admit` and the fail-open
handlers never ran. Measured: no answer after 5 s with no budget; exactly the
budget, as a `TimeoutError` the handlers already catch, with one.
Options: A) socket and connect timeouts on the client, from settings
B) `asyncio.wait_for` around each consumer's calls C) a breaker in front of
Redis that skips it while degraded D) leave it to the operator's URL query string.
Decision: A, at 0.25 s per call and 0.5 s to connect. A healthy Redis answers
in about a millisecond, and one request makes about five Redis calls in
sequence, each spending the whole budget while Redis is sick, so the budget is
what a degraded request costs five times over. B repeats one number at every
call site and cancels mid-protocol, leaving a connection the pool must then
discard. D is a default nobody sets. C is not built: it would make a sick Redis
cost nothing rather than about 1.25 s, but it is a second breaker with its own
state per worker, and bounded is the property that was missing.
Consequences: A Redis stall is extra latency on each request instead of an
outage. A Redis that is slow but working — a fork for `BGSAVE` on a large
dataset — now fails open for its duration rather than being waited for, which
lets requests through unmetered. Revisit with C if a degraded Redis's ~1.25 s
a request shows up in the latency SLO, or raise the budgets if Redis lives
across a link where 0.25 s is not generous.
