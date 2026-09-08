"""``vortex-keys`` — mint, list and revoke client API keys.

Deliberately not an HTTP endpoint. Creating the *first* credential over an
authenticated API is a bootstrapping problem, and the usual escape — an admin
route behind a separate bootstrap secret — is the plaintext environment key we
just replaced, wearing a hat. An operator who can mint keys already has the
database file; the CLI reaches it directly and the gateway grows no new surface
(ADR-003).

    vortex-keys create --name ci-pipeline --rpm 600 --tpm 150000
    vortex-keys list
    vortex-keys revoke 9f2c41ab77de

The database comes from ``VORTEX_KEY_DB_PATH`` (via the usual settings, so a
``.env`` works) unless ``--db`` overrides it.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.keys import ApiKeyRecord, KeyStore

#: Printed with the token, because it is true exactly once.
CREATED_NOTICE = "Store this token now. Only its hash is kept, so it cannot be shown again."


def _timestamp(value: float) -> str:
    """A UTC timestamp an operator can read."""
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _limit(value: int) -> str:
    """A limit, where zero means "whatever the deployment's default is"."""
    return str(value) if value else "default"


def _format_row(record: ApiKeyRecord) -> str:
    # Narrowed rather than defaulted: `revoked_at` is only ever a timestamp when
    # `revoked` is true, and a `_timestamp(None)` branch would be unreachable.
    revoked_at = record.revoked_at
    status = "active" if revoked_at is None else f"revoked {_timestamp(revoked_at)}"
    return (
        f"{record.key_id}  {_timestamp(record.created_at)}  "
        f"rpm={_limit(record.rpm)}  tpm={_limit(record.tpm)}  "
        f"{status}  {record.name or '-'}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vortex-keys", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite file holding the keys. Defaults to VORTEX_KEY_DB_PATH.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="mint a key and print it once")
    create.add_argument("--name", default="", help="what this key is for, e.g. 'ci-pipeline'")
    create.add_argument(
        "--rpm", type=int, default=0, help="requests per minute; 0 uses the deployment default"
    )
    create.add_argument(
        "--tpm", type=int, default=0, help="tokens per minute; 0 uses the deployment default"
    )

    listing = commands.add_parser("list", help="show every key, newest first")
    listing.add_argument("--active", action="store_true", help="hide keys that have been revoked")

    revoke = commands.add_parser("revoke", help="retire a key by its public ID")
    revoke.add_argument("key_id")

    return parser


def _resolve_db(explicit: str | None) -> str | None:
    """Where the keys live: the flag, else the environment, else nowhere."""
    if explicit:
        return explicit
    return Settings().key_db_path or None


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return the process exit status.

    Returns a status rather than calling :func:`sys.exit` so the whole CLI is
    testable in-process; ``console_scripts`` turns the return value into the
    exit code for us.
    """
    args = _build_parser().parse_args(argv)

    database = _resolve_db(args.db)
    if database is None:
        print(
            "No key database configured. Pass --db PATH or set VORTEX_KEY_DB_PATH.",
            file=sys.stderr,
        )
        return 2
    store = KeyStore(database)

    if args.command == "create":
        minted = store.create(name=args.name, rpm=args.rpm, tpm=args.tpm)
        print(minted.token)
        print(CREATED_NOTICE, file=sys.stderr)
        return 0

    if args.command == "revoke":
        record = store.revoke(args.key_id)
        if record is None:
            print(f"No key with ID {args.key_id}.", file=sys.stderr)
            return 1
        print(f"Revoked {record.key_id} ({record.name or 'unnamed'}).")
        return 0

    records = store.list_keys(include_revoked=not args.active)
    if not records:
        print("No keys.", file=sys.stderr)
        return 0
    for record in records:
        print(_format_row(record))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through `main`
    raise SystemExit(main())
