# ADR-027: A metric label a caller controls is capped, and the overflow is named
Date / Status: 2026-09-22 / accepted
Context: `model` is the most useful breakdown on every metric this gateway
publishes — requests, latency, tokens, dollars — and it is a free-text string in
a request body that anyone with a key can set. A Prometheus label value
allocates a time series that lives until the process exits and is never freed,
so a client looping over random model names turns the monitoring system into the
outage it was installed to detect. `route` has the same shape: built from the
request line it is one series per URL a scanner guesses at.
Options: A) label with the model as sent and accept unbounded growth B) drop the
label, and lose the breakdown everyone actually wants C) admit a fixed number of
distinct values per process and report everything after that as `other`
D) accept only models that appear in the routing table, and call the rest
`unrouted`. Separately: whether cache outcomes are counted by `cache.py` and
`semantic.py` or read off the response headers.
Decision: C, with the cap as a setting (`VORTEX_METRICS_LABEL_BUDGET`, 50), and
`route` labelled from the *matched route template* with one shared `unmatched`
for everything else. D was rejected because it does not work for the deployments
that need it most — a gateway on its mock provider, or one with a glob rule like
`gpt-*=openai`, has no finite list to check against — and it makes a metric
depend on the routing table, which is a second reason for a dashboard to change
when nobody touched the dashboard. `LabelBudget` is about twenty lines and keeps
values it has already admitted forever, so the breakdown stays stable once
traffic settles. Cache outcomes are read off the outgoing `X-Cache` and
`X-Semantic-Cache` headers in the same middleware, exactly as the access log
reads them, so there is one definition of "a hit" and it is the one the caller
was told; `cache.py` and `semantic.py` keep knowing nothing about being
observed. A tier that is not running emits no header and therefore no series at
all, rather than a run of zeros that would drag somebody else's hit rate down.
Consequences: The worst case is a number an operator picked rather than one a
caller picked. The weakness is the mirror image: a model introduced *after* the
budget fills reports as `other` until the process restarts, and on a dashboard
that looks like the new model getting no traffic. `other` appearing at all is
the signal to raise the budget. Span attributes are deliberately left uncapped —
an attribute is a string on one span, not a series that outlives it — so a trace
always names the real model even when the metric says `other`. Revisit if the
label set ever needs to be per-tenant, which multiplies rather than adds, or if
a `/metrics` scrape grows large enough that the series count is itself the cost.
