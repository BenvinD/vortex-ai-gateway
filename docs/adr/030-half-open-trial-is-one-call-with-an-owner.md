# ADR-030: The half-open trial is one call, and only the permit holding it can hand it back
Date / Status: 2026-10-18 / accepted
Context: ADR-002's single trial is a slot that `allow()` reserves, and a slot
nobody returns leaves the breaker in `HALF_OPEN` refusing every caller with no
clock running — permanently, per worker. Only cancellation returned it. A trial
that failed retryably was refused by its *own* reservation when it retried (a
`503` with no `Retry-After`, slot kept); a non-retryable failure and any
exception that is not a `ProviderError` kept it too. The first is the expected
case, the provider still down at the first probe, so any outage longer than one
open window wedged that provider until restart. And `release_trial()` cleared
the slot for whoever called it, so a call admitted while closed and ending
during half-open freed the real trial's slot and let a second trial run.
Options: A) the trial keeps its retries, holding the slot across them B) the
slot is handed back between attempts C) the trial is one call, endings that
learned nothing release without a verdict, and `allow()` returns a `Permit`
that only its holder can release.
Decision: C. A retryable trial failure reopens at once for the escalated window
and gives up under its own reason, `half-open trial failed`; the caller gets the
real `502`/`504`. A non-retryable failure, a cancellation and a non-provider
exception hand the slot back in one `finally` per attempt and move nothing —
the rule ADR-002 already applies while closed. Ownership is identity (`Permit`
is `eq=False`), so a closed-era call and last window's trial release nothing.
A refuses every other caller while the trial sleeps through backoff, up to the
deadline, and gives a dead provider three calls a window; B makes one probe
several calls — and is what the `finally` alone does, which is why a test pins
the trial at exactly one upstream call. Counting no-information endings as a
failed trial lets one client's `400` extend an outage, and a deterministic
adapter bug would cycle the breaker open forever. Treating any vendor answer as
proof of life needs a "reached the wire" flag the taxonomy does not have. A
`bool` plus `try/finally` keeps the cross-release; an `async with` admission
moves every verdict into a block per attempt for the same guarantee.
Consequences: Recovery is one call per window and a probe can no longer wedge
its own breaker; an ending that learned nothing costs one more trial, taken by
the next caller. Deliberately not built: callers refused while a trial is in
flight still get a `503` with no `Retry-After`, because there is no clock to
report, only a call whose length is the provider's; and a failure from a call
admitted before the breaker opened still lands as a verdict during half-open.
Revisit if a provider's first call after idle reliably fails — scale-to-zero,
cold starts — because then one call is not a fair probe.
