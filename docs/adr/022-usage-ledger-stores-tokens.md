# ADR-022: The ledger stores token counts; money is computed at read time
Date / Status: 2026-09-06 / accepted
Context: `StreamRecord` (ADR-018) already knows what each request cost in
tokens, but only as a log line nobody adds up. Callers need to see their own
spend, and the gateway needs a number to reconcile against a vendor invoice.
Options: A) store dollars as floats B) store dollars as integer nano-USD
C) store integer token counts per key per model per UTC day, and price them when
someone asks.
Decision: C. Floats cannot represent $2.50 per million tokens, and summing a few
million of them produces a figure that is nearly the invoice — close enough that
nobody checks it, different enough to argue about. Nano-USD fixes the precision
and keeps the real problem: a stored price is frozen at whatever the table said
that day, so correcting a wrong price needs a migration rather than an edit.
Token counts are exact under `HINCRBY`, are what a vendor itemises, and reprice
the whole history the next time someone asks. Prices live in a built-in table
with a JSON file layered in front (`VORTEX_PRICE_TABLE_PATH`), because vendor
prices change more often than this repository does. An unpriced model returns
`None`, never zero — a new model silently costing nothing is how it gets rolled
out and billed to nobody — and `/v1/usage` names what it could not price so the
total explains itself. A request whose usage never arrived (an abandoned stream,
ADR-019) is recorded as *unmetered*: counted, published separately, never
reported as free. `/v1/usage` answers only about the calling key; a report that
can name another key is an authorisation system, and there isn't one.
Consequences: Repricing is a file edit. Reports cost one `HGETALL` per day of
window, and retention is a TTL rather than a sweep. The counters are Redis, so
they are as durable as Redis — fine for "what have I spent this week", not a
system of record for invoicing, which would want the `request settled` log lines
shipped somewhere durable. Storage is per key per model per day, so there is no
way to ask "what did request X cost"; the log line is where that lives.
