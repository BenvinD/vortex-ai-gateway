# ADR-015: Provider failures are classified by whether a retry could work
Date / Status: 2026-09-02 / accepted
Context: An adapter fails in ways that want opposite responses: a timeout worth
retrying, a `429` worth retrying *later*, a `400` no retry will ever fix, and a
`401` that is a deployment fault dressed as an auth error. The retry policy
(ADR-001), the breaker (ADR-002) and the HTTP status all have to branch on this,
and branching on `str(exc)` or a raw status code is how each of them acquires
its own subtly different opinion.
Options: A) let httpx exceptions and status codes escape, and let each consumer
classify B) one `ProviderError` carrying a status code C) a typed hierarchy —
`ProviderTimeout`, `ProviderRateLimited`, `ProviderBadRequest`,
`ProviderUnavailable`, plus `ProviderAuthError` and `ProviderProtocolError` —
each with a `retryable` class attribute.
Decision: C, classified once in `HttpChatAdapter` so three adapters cannot
disagree about whether a `529` is retryable. `retryable` is an attribute rather
than a rule each consumer re-derives. The two extra classes exist because
folding them in would make a lie: a `401` means *our* key is wrong, so reporting
it as the caller's bad request sends them to rotate a key that is fine, and an
unreadable body is contract drift that a retry reproduces exactly.
`TranslationError` subclasses `ProviderBadRequest`, since a request we refuse to
send is a bad request we caught first. Providers still raise rather than
returning an envelope (ADR-011): the status mapping lives in `routes.py`, which
is also where `429` forwards the provider's own `Retry-After`.
Consequences: Retry and breaker logic can key on a type, and a status
reclassified from retryable to not is a visible one-line diff with a test on it.
The cost is that every new failure source has to be classified deliberately —
an unclassified exception falls through as a `502`, which is safe but silent.
