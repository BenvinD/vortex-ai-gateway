# ADR-003: Client keys live hashed in SQLite, minted by a CLI
Date / Status: 2026-09-06 / accepted
Context: Keys were a comma-separated plaintext list in the environment
(ADR-013's stub). Revoking one meant a redeploy, the secret was readable in
every `env` dump and deployment console, and there was nothing durable to hang a
per-key rate limit or bill on — which the next two decisions both need.
Options: A) keep the env list and add rotation tooling B) Postgres C) SQLite
through the standard library, one file, connection per operation.
Decision: C, with three choices inside it. **Storage**: SQLite is a stdlib
import and a file, so nothing new runs; Postgres is the right answer for a
fleet and buys nothing until there is one. **Hash**: SHA-256, not bcrypt or
argon2 — a work factor exists to make *guessable* secrets expensive to guess,
and there is no dictionary behind 32 CSPRNG bytes, so it would buy nothing and
cost ~100 ms of CPU per authenticated request. **Shape**: `vtx_<key_id>_<secret>`,
looked up by the public `key_id` and confirmed with `hmac.compare_digest`, so a
missing row and a wrong secret take the same path and timing does not leak which
IDs exist. The prefix is deliberate: secret scanners match on known prefixes, so
a leaked key that announces itself is caught faster than an anonymous blob.
Minting is a CLI, not an endpoint, because creating the *first* credential over
an authenticated API is a bootstrap problem whose usual answer — an admin route
behind a bootstrap secret — is the plaintext env key wearing a hat. A configured
store *replaces* the env list rather than merging with it; two allow-lists means
revoking from one and still being let in by the other.
Consequences: Revocation is a row update that takes effect on the next request.
The database is one file on one node, so a multi-node deployment either shares
it (NFS: don't) or waits for the rejected option — Postgres, with the same
schema and the same hash, which is why the store is a class and not a module of
functions. Every authenticated request does a SQLite read on a thread from the
default executor; that is fine at one read per request and is the first thing to
cache if it stops being fine.
