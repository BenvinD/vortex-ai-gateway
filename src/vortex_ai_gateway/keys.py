"""Client API keys: minted, hashed, stored, revoked.

The gateway's first credential store. It replaces the plaintext allow-list in
:attr:`~vortex_ai_gateway.config.Settings.api_keys` — which is kept, because a
local run should not need a database — with keys that can be revoked without a
redeploy and that are never stored in a form an attacker could present.

Three decisions worth knowing before reading the code (ADR-003):

**A token is three parts.** ``vtx_<key_id>_<secret>``. The ``key_id`` is public:
it is the handle an operator revokes by, the label that appears in logs, and the
bucket a rate limit and a bill hang off. The ``secret`` is 32 CSPRNG bytes and
is shown exactly once, at creation. The prefix makes a leaked key greppable —
scanners look for known prefixes, and a token that announces what it is gets
caught in a public repository faster than an anonymous blob.

**The hash is SHA-256, not bcrypt.** Password hashes are slow on purpose,
because a human-chosen password has a dictionary behind it and the defence is to
make each guess expensive. There is no dictionary behind
:func:`secrets.token_urlsafe`; an attacker has to search 2^256, and multiplying
that by a work factor is meaningless. What a work factor *would* cost is real:
~100 ms of CPU on every authenticated request, on the hot path, for nothing.

**Lookup is by ``key_id``, then a constant-time compare.** Not "hash the token
and search for it": that works, but this way a missing row and a wrong secret
take the same code path, including the compare, so response time does not tell
an attacker which key IDs exist.

SQLite is reached through :mod:`sqlite3`, which is blocking; every call from the
request path goes through :func:`asyncio.to_thread`. Connections are opened per
operation rather than pooled — authentication happens once per request, not once
per token, and the CLI writes to the same file from a different process.
"""

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Marks a string as one of ours, for humans and for secret scanners alike.
TOKEN_PREFIX: Final = "vtx"

#: Bytes of entropy in the secret half. 32 is the point past which the hash,
#: not the token, is the weakest thing in the chain.
SECRET_BYTES: Final = 32

#: Bytes in the public identifier. Short enough to paste into a revoke command,
#: long enough that IDs do not collide before the heat death of the service.
KEY_ID_BYTES: Final = 6

#: How long a busy writer waits for the CLI's lock before giving up, in ms.
#: SQLite's default is zero, which turns a concurrent write into an immediate
#: "database is locked" rather than a wait of a few milliseconds.
BUSY_TIMEOUT_MS: Final = 5_000

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id      TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    revoked_at  REAL,
    rpm         INTEGER NOT NULL DEFAULT 0,
    tpm         INTEGER NOT NULL DEFAULT 0
)
"""


def fingerprint(token: str) -> str:
    """A stable, non-reversible label for a token with no record behind it.

    The environment allow-list and the open development mode still need an
    identity to meter and bill against, and it must not be the secret itself:
    that identity ends up in Redis keys and log lines. A truncated digest is
    neither, and it is stable across restarts, which a random ID would not be.
    """
    return hashlib.sha256(token.encode()).hexdigest()[: KEY_ID_BYTES * 2]


def hash_token(token: str) -> str:
    """The stored form of a token. See the module docstring for why SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()


def parse_key_id(token: str) -> str | None:
    """The public half of a well-formed token, or ``None`` if it is not one.

    A key from the environment allow-list is not one — it has no structure at
    all — so this returning ``None`` is a routine answer, not an error.

    Split at most twice, and the reason is a bug this cost us once already:
    :func:`secrets.token_urlsafe` emits base64url, whose alphabet *includes*
    the underscore. A plain ``split("_")`` therefore returns four parts for
    roughly half the tokens ever minted, which is not a broken key — it is a
    key that works until the day it does not.
    """
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    key_id, secret = parts[1], parts[2]
    if not key_id or not secret:
        return None
    return key_id


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """Everything about a key except the one thing we do not keep: the secret."""

    key_id: str
    name: str
    created_at: float
    revoked_at: float | None = None
    rpm: int = 0
    tpm: int = 0

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True, slots=True)
class MintedKey:
    """A freshly created key, and the only moment its token exists in memory."""

    record: ApiKeyRecord
    token: str


class KeyStore:
    """A SQLite-backed table of hashed client keys.

    Construction creates the file and the schema if they are absent, so the CLI
    and the app can both be pointed at a path that does not exist yet and the
    first one there wins.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent != Path():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection, one operation, committed or rolled back on the way out."""
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        with closing(connection), connection:
            yield connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ApiKeyRecord:
        return ApiKeyRecord(
            key_id=row["key_id"],
            name=row["name"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
            rpm=row["rpm"],
            tpm=row["tpm"],
        )

    def create(self, *, name: str = "", rpm: int = 0, tpm: int = 0) -> MintedKey:
        """Mint a key, store its hash, and return the token for its only showing.

        ``rpm`` and ``tpm`` of zero mean "take the deployment's default", which
        may itself be unlimited; see
        :meth:`~vortex_ai_gateway.auth.Principal` for how the ladder resolves.
        """
        key_id = secrets.token_hex(KEY_ID_BYTES)
        token = f"{TOKEN_PREFIX}_{key_id}_{secrets.token_urlsafe(SECRET_BYTES)}"
        record = ApiKeyRecord(key_id=key_id, name=name, created_at=time.time(), rpm=rpm, tpm=tpm)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO api_keys (key_id, token_hash, name, created_at, rpm, tpm)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, hash_token(token), name, record.created_at, rpm, tpm),
            )
        return MintedKey(record=record, token=token)

    def revoke(self, key_id: str) -> ApiKeyRecord | None:
        """Retire a key, returning its record — or ``None`` if there is no such key.

        Revoking is a timestamp, not a ``DELETE``: the ID stays resolvable, so a
        request that arrives after revocation is a *revoked key* in the logs
        rather than an unknown one, and the ledger keeps the spend attached to
        something with a name. Revoking twice keeps the first timestamp, because
        that is when the key stopped working.
        """
        with self._connect() as connection:
            connection.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
                (time.time(), key_id),
            )
            row = connection.execute(
                "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def list_keys(self, *, include_revoked: bool = True) -> list[ApiKeyRecord]:
        """Every key, newest first."""
        clause = "" if include_revoked else " WHERE revoked_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_keys{clause} ORDER BY created_at DESC"
            ).fetchall()
        return [self._record(row) for row in rows]

    def verify(self, token: str) -> ApiKeyRecord | None:
        """The live key this token authenticates, or ``None``.

        ``None`` covers all three failures on purpose — the token is not ours,
        no such key, wrong secret — because the caller is told the same thing in
        every case. The key ID it named belongs in the gateway's logs, not in
        the 401.

        A token with no matching row is still compared against a dummy hash of
        the *right length*, so the work done is the same either way.
        ``hmac.compare_digest`` short-circuits on a length mismatch, so comparing
        against ``""`` would make the missing-row path measurably faster than the
        wrong-secret one — which is the leak the constant-time compare is here to
        close.
        """
        key_id = parse_key_id(token)
        if key_id is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()

        stored = row["token_hash"] if row is not None else hash_token("")
        matches = hmac.compare_digest(stored, hash_token(token))
        if row is None or not matches or row["revoked_at"] is not None:
            return None
        return self._record(row)

    async def authenticate(self, token: str) -> ApiKeyRecord | None:
        """:meth:`verify`, off the event loop.

        SQLite is blocking. One authenticated request is one file read of a few
        microseconds, but "a few microseconds" is a statement about a warm page
        cache, and a cold read on a loaded host is not — and it would stall
        every other request in flight on this worker.
        """
        return await asyncio.to_thread(self.verify, token)
