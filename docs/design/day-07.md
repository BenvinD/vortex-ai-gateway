## Day 07 — who is calling, how much they may spend, and what it cost

**Building.** The gateway currently authenticates against a comma-separated
list of plaintext keys in an environment variable, admits every request that
survives that check, and knows what each stream cost only as a log line nobody
adds up. Four gaps, and they are one gap: there is no *caller identity* durable
enough to hang a limit or a bill on. So: real keys, stored hashed; a per-key
request/token limiter; and a running spend a caller can read back.

**Alternatives — key storage.** (1) Keep the env list and add rotation tooling:
no new dependency, but revocation means a redeploy, and the plaintext key is in
the process environment, the deployment console, and every `env` dump.
(2) Postgres: the right answer for a fleet, and a server to run, a driver to
add, and a migration tool to pick before a single key exists.
(3) SQLite through the standard library, one file, connection per operation.
Chose (3) — see ADR-003. The interesting decision inside it is the hash:
SHA-256, not bcrypt or argon2. Those exist to make *guessable* secrets
expensive to guess; a 256-bit CSPRNG token has no dictionary behind it, so a
work factor buys nothing and costs ~100 ms of CPU on every authenticated
request.

**Alternatives — where keys are minted.** (1) A `POST /v1/keys` endpoint:
convenient, and it needs a key to create the first key. (2) An admin endpoint
behind a separate bootstrap secret: two auth systems, and the bootstrap secret
is the plaintext env key we just removed, wearing a hat. (3) A CLI that talks
to the database directly, `vortex-keys create|list|revoke`. Chose (3): minting
a credential is an operator action, it already has filesystem access, and no
HTTP surface means no HTTP surface to get wrong.

**Alternatives — the limiter.** (1) Per-process counters: no Redis, and N
workers means N× the limit, which is not a limit. (2) Redis `INCR` with a
per-minute key: one round trip, but it is a fixed window — a caller gets 2× the
limit across the boundary, and it cannot express "requests *and* tokens, admit
only if both fit". (3) A token bucket in a Lua script, both buckets checked and
debited in one atomic call. Chose (3) — see ADR-021. Atomicity is the whole
requirement: if the request bucket admits and the token bucket refuses, the
request must not have spent a request.

**The awkward part: TPM is a limit on something not yet known.** A request's
token cost exists only after the provider answers. Three ways out. (1) Charge
after the fact: the limit is then advisory, and one enormous request blows
through it before anything can say no. (2) Count prompt tokens only: cheap, and
completions are the expensive half. (3) Reserve an estimate up front, reconcile
against the real usage when the request settles. Chose (3). The estimate is
deliberately dumb — `effective_max_tokens` when the caller set it, otherwise
`len(text) // 4` plus a configured default — because a good estimator is a
tokenizer per vendor, and the reconciliation makes the estimate's *accuracy*
matter far less than its existence. The refund is clamped at bucket capacity,
or an over-estimate would refund a bucket to more than full.

**Alternatives — the ledger.** (1) Store dollars: floats lose cents, and a
price-table correction silently rewrites history. (2) Store dollars as integer
nano-USD: exact, still frozen at the price in force when the request ran.
(3) Store token counts as integers, per key per model per day, and price at
*read* time. Chose (3) — see ADR-022. Money never enters storage, `HINCRBY` is
exact, and repricing is a table edit rather than a migration.

**Planned experiment.** Fire 200 concurrent requests at a bucket of 50 against
a Redis executing the real Lua, and count admissions. Prediction: exactly 50,
and 150 rejections carrying `Retry-After`. The number to watch is *over* 50 —
that would mean the check and the debit are separate round trips and something
interleaved between them, which is the bug the Lua exists to prevent and the
one a single-threaded test would never show.
