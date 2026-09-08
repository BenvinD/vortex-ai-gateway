# ADR-017: Configuration stays in the environment; no embedded database
Date / Status: 2026-09-02 / accepted
Context: The routing table joins API keys, base URLs and timeouts as things the
gateway must be told. The question raised was whether that has outgrown flat
config and wants an embedded document store (LiteDB) instead of plain
env/JSON.
Options: A) environment variables read once at boot (ADR-007's existing choice)
B) a JSON or YAML file mounted beside the app C) an embedded document database
written at runtime (LiteDB, SQLite, TinyDB).
Decision: A, unchanged. Three findings settle it. LiteDB is a .NET single-file
store with no official Python driver — the same-named PyPI package is an
unrelated project — so "LiteDB" here would in practice mean SQLite, and the
question becomes "a schema and migrations, for ten rows read once at startup?".
The closest prior art, LiteLLM, ships a YAML file by default and only adds a
database when day-2 edits must land through an admin UI *without a restart* —
and it reaches for Postgres, a shared server, not an embedded file. That is the
trigger to watch for, and the reason C is wrong even when the trigger fires: a
per-replica file gives every replica its own private routing table. Finally the
table sits next to the API keys it pairs with, and those belong in the
environment (or a secret manager), never in a file a database driver can print.
Consequences: A routing change needs a restart, which is acceptable while the
table changes weekly and unacceptable once it changes hourly. B is the cheap
next step if the table outgrows one variable — same parser, different source.
When runtime mutation is genuinely needed, the store is Redis: already a
declared dependency, already the coordination point for rate limiting and load
balancing, and shared across replicas by construction. That is a change to
`build_router` alone; nothing above it knows where the table came from.
