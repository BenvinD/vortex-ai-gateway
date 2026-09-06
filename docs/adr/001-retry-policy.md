# ADR-001: Retries branch on the taxonomy, with full jitter and a real deadline
Date / Status: 2026-09-06 / accepted
Context: An upstream blip should not reach the caller, but retrying the wrong
failure is worse than not retrying: a `400` fails identically every time, and a
`401` is our own misconfiguration. Retries are also a load multiplier — during
an outage, every client retrying three times triples the offered load on the
provider least able to take it.
Options: A) fixed retry count for every failure B) branch on HTTP status
C) branch on `ProviderError.retryable` (ADR-015), full jitter, a wall-clock
deadline, and an optional aggregate retry budget.
Decision: C. `retryable` is read as an attribute, never re-derived, so a
reclassification is one line in `errors.py` with a test on it. Backoff is *full*
jitter — uniform over `[0, base·2^n]`, not base-plus-noise — because
deterministic backoff re-synchronises every client that failed together into a
herd. A provider's own `Retry-After` always beats our arithmetic. The deadline
(`VORTEX_RETRY_DEADLINE_SECONDS`, default 90s) covers the whole sequence and
must exceed `request_timeout_seconds`: set equal, the first slow failure spends
it all and `max_attempts` silently means one — for timeouts, the failure most
worth retrying. The budget (a token bucket, off by default) caps retries in
aggregate rather than per request.
Consequences: A caller's own bad request costs exactly one upstream call, and a
transient one is usually invisible. Two numbers now have to be kept in a
relationship — deadline > timeout — which a config validator should eventually
enforce rather than a comment. Streams are not retried at all: past the first
chunk the `200` is committed, and a retry would splice two generations into one
body. We revisit if establishing-phase stream retries turn out to matter.
