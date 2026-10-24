# ADR-013: Bearer-token client auth at the router, stubbed for now
Date / Status: 2026-09-01 / accepted; storage superseded by ADR-003 (2026-09-06)
Context: The `/v1` surface must not be open, but real key management — hashing,
storage, scopes, rotation, per-key limits (ADR-003) — is a larger piece of work
than the endpoint it protects. What cannot be deferred is *where* auth happens
and what a rejection looks like, because both are hard to retrofit.
Options: A) leave the endpoint open until key storage exists B) a
`require_api_key` dependency declared on the router, checking
`Authorization: Bearer <key>` against a comma-separated `VORTEX_API_KEYS`
allow-list, and accepting any well-formed key when none is configured C) hand
the whole problem to an API gateway or service mesh in front of this one.
Decision: B. The dependency is declared on the `APIRouter`, not per route, so a
route added later cannot be published unauthenticated by omission; the health
probes stay open because they live on the app instead. A rejection is `401` with
`WWW-Authenticate: Bearer` and the same `{"error": {...}}` envelope every other
failure uses, typed `authentication_error`. A missing key and an unreadable
header get distinct codes — they are different bugs at the caller. Auth runs
before body validation, so an unauthenticated caller learns nothing about the
payload.
Consequences: With no keys configured the gateway accepts any key, which is a
development convenience and a production hazard; the empty-allow-list branch
must go when ADR-003 lands.
Update (2026-09-08): ADR-003 landed and the branch did **not** go, deliberately.
`VORTEX_API_KEYS` survives as the one thing it is good at — a local run or a
single-key deployment that should not need a database — and development mode
(neither source configured) survives because it still rejects a request carrying
*no* key, so the 401 path is exercised by default rather than discovered in
production. What did change is that the three sources are never merged and a
configured store wins outright. The hazard is unchanged and now belongs to
deployment: a `prod` environment with neither source set is the check this repo
still owes. Keys are compared in plaintext and are not scoped,
rate-limited, or attributable to a tenant yet. C stays viable and would make
this redundant, at the cost of the gateway not being deployable on its own.
Update (2026-10-24): "Auth runs before body validation" was true of the schema
and not of the JSON. FastAPI reads the whole body, and decodes it when its
`Content-Type` is JSON, before it resolves any dependency; schema errors are
collected and raised after them. So with no key, a body labelled JSON that does
not parse is a `400`, while one labelled anything else, or JSON that fails the
schema, is still a `401`. The intent holds — an unauthenticated caller learns nothing
about whether a payload is *valid*, only that its bytes were not JSON, which it
already knew — so this is recorded rather than fixed; moving auth into ASGI
middleware to restore the old wording would buy nothing for that. What the
wording hid is cost: every request's body is read in full before anyone checks
the key, and nothing capped its size until ADR-034. `tests/test_auth.py` pins the ordering.
