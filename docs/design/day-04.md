## Day 04 — routing, and what a failure *is*

**Building.** A model → provider routing table that lives in configuration, and
a failure taxonomy the retry policy can branch on without reading messages.
Both are prerequisites for Day 5's retries: retrying is meaningless until
something can say "this failure is worth retrying" and "here is another
provider that serves this model".

**Alternatives — routing.** (1) A dict of exact model names: simple, but every
new snapshot of a model is a config edit. (2) An ordered list of glob rules,
first match wins: expressive enough for `gpt-*` with a `gpt-4o` exception above
it. (3) Regexes: more power than routing needs, and far more ways to be wrong
in a deployment console. Chose (2) — see ADR-016.

**Alternatives — failures.** (1) Let httpx exceptions and status codes escape,
and classify at each consumer: guarantees three consumers with three opinions.
(2) One error class carrying a status: the branching just moves into `if
exc.status_code in (429, 500, 502...)`, copied everywhere. (3) A typed hierarchy
with a `retryable` flag, classified once. Chose (3) — see ADR-015.

**Alternatives — storage.** LiteDB, SQLite, a JSON file, or the environment.
Researched and settled in ADR-017: the environment, because ten rows read once
at boot do not want a schema, and because an embedded per-replica file is the
one option that is *wrong at scale* rather than merely unnecessary.

**Planned experiment.** Point an adapter at a black-holed IP and find out which
timeout actually fires, and after how long.
