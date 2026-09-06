"""Tests for the hashed API-key store and the `vortex-keys` CLI."""

import sqlite3
import threading
from pathlib import Path

import pytest

from vortex_ai_gateway import cli
from vortex_ai_gateway.keys import (
    TOKEN_PREFIX,
    ApiKeyRecord,
    KeyStore,
    fingerprint,
    hash_token,
    parse_key_id,
)


@pytest.fixture
def store(tmp_path: Path) -> KeyStore:
    """A key store in a throwaway file, created by the constructor."""
    return KeyStore(tmp_path / "keys.sqlite3")


# --- minting -----------------------------------------------------------------


def test_a_minted_token_names_its_own_key_id(store: KeyStore) -> None:
    """The public half is readable from the token, which is what makes lookup O(1)."""
    minted = store.create(name="ci")

    assert minted.token.startswith(f"{TOKEN_PREFIX}_{minted.record.key_id}_")
    assert parse_key_id(minted.token) == minted.record.key_id


def test_the_secret_is_never_stored(store: KeyStore) -> None:
    """A database dump must not contain anything a caller could present."""
    minted = store.create()

    with sqlite3.connect(store.path) as connection:
        dump = "\n".join(connection.iterdump())

    assert minted.token not in dump
    assert hash_token(minted.token) in dump


def test_two_keys_never_collide(store: KeyStore) -> None:
    """Both halves come from the CSPRNG; neither is derived from the other."""
    tokens = {store.create().token for _ in range(50)}
    ids = {parse_key_id(token) for token in tokens}

    assert len(tokens) == 50
    assert len(ids) == 50


def test_per_key_limits_are_stored_and_returned(store: KeyStore) -> None:
    """A key carries its own limits so one noisy client can be capped alone."""
    minted = store.create(name="batch", rpm=600, tpm=150_000)

    verified = store.verify(minted.token)

    assert verified == ApiKeyRecord(
        key_id=minted.record.key_id,
        name="batch",
        created_at=minted.record.created_at,
        rpm=600,
        tpm=150_000,
    )


# --- verification ------------------------------------------------------------


def test_a_good_token_verifies(store: KeyStore) -> None:
    minted = store.create(name="ci")

    record = store.verify(minted.token)

    assert record is not None
    assert record.key_id == minted.record.key_id
    assert record.name == "ci"


@pytest.mark.parametrize(
    ("token", "why"),
    [
        ("not-a-vortex-token", "no structure at all"),
        ("vtx_deadbeef", "too few parts"),
        ("vtx__secret", "empty key id"),
        ("vtx_deadbeef_", "empty secret"),
        ("sk_deadbeef_secret", "someone else's prefix"),
    ],
)
def test_a_malformed_token_is_refused_without_a_lookup(
    store: KeyStore, token: str, why: str
) -> None:
    """Nothing that is not shaped like one of ours reaches the database."""
    assert parse_key_id(token) is None, why
    assert store.verify(token) is None


def test_the_right_id_with_the_wrong_secret_is_refused(store: KeyStore) -> None:
    """The ID is public; presenting it is not the same as holding the key."""
    minted = store.create()

    forged = f"{TOKEN_PREFIX}_{minted.record.key_id}_wrong-secret"

    assert store.verify(forged) is None


def test_an_unknown_key_id_is_refused(store: KeyStore) -> None:
    """A well-formed token for a key that was never minted."""
    assert store.verify(f"{TOKEN_PREFIX}_000000000000_whatever") is None


# --- revocation --------------------------------------------------------------


def test_a_revoked_key_stops_verifying(store: KeyStore) -> None:
    minted = store.create(name="leaked")
    assert store.verify(minted.token) is not None

    revoked = store.revoke(minted.record.key_id)

    assert revoked is not None
    assert revoked.revoked is True
    assert store.verify(minted.token) is None


def test_revoking_keeps_the_row(store: KeyStore) -> None:
    """The ID stays resolvable, so the spend attached to it keeps a name."""
    minted = store.create(name="leaked")

    store.revoke(minted.record.key_id)

    listed = store.list_keys()
    assert [record.key_id for record in listed] == [minted.record.key_id]
    assert listed[0].name == "leaked"


def test_revoking_twice_keeps_the_first_timestamp(store: KeyStore) -> None:
    """When it stopped working is a fact, not the last time someone asked."""
    minted = store.create()

    first = store.revoke(minted.record.key_id)
    second = store.revoke(minted.record.key_id)

    assert first is not None and second is not None
    assert first.revoked_at == second.revoked_at


def test_revoking_a_key_that_does_not_exist_says_so(store: KeyStore) -> None:
    assert store.revoke("000000000000") is None


def test_listing_can_hide_revoked_keys(store: KeyStore) -> None:
    live = store.create(name="live")
    dead = store.create(name="dead")
    store.revoke(dead.record.key_id)

    active = store.list_keys(include_revoked=False)

    assert [record.key_id for record in active] == [live.record.key_id]
    assert len(store.list_keys()) == 2


# --- identity for keys with no record ----------------------------------------


def test_a_fingerprint_is_stable_and_is_not_the_token() -> None:
    """Environment-list keys still need something safe to meter and bill against."""
    first = fingerprint("plaintext-key")
    second = fingerprint("plaintext-key")

    assert first == second
    assert first != fingerprint("another-key")
    assert "plaintext-key" not in first


# --- the async seam ----------------------------------------------------------


async def test_authenticate_runs_the_lookup_off_the_event_loop(
    store: KeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request path never blocks the loop on a file read.

    Asserted by where the work happens rather than by how long it takes: a
    timing test for this would pass on a warm page cache no matter what.
    """
    minted = store.create(name="ci")
    verify = store.verify
    ran_on: list[str] = []

    def spy(token: str) -> ApiKeyRecord | None:
        ran_on.append(threading.current_thread().name)
        return verify(token)

    monkeypatch.setattr(store, "verify", spy)

    record = await store.authenticate(minted.token)

    assert record is not None
    assert ran_on and ran_on[0] != threading.current_thread().name
    assert await store.authenticate("nope") is None


# --- reopening ---------------------------------------------------------------


def test_a_second_store_over_the_same_file_sees_the_same_keys(tmp_path: Path) -> None:
    """The CLI writes from another process; the schema must not be recreated blank."""
    path = tmp_path / "keys.sqlite3"
    minted = KeyStore(path).create(name="ci")

    assert KeyStore(path).verify(minted.token) is not None


def test_the_store_creates_missing_parent_directories(tmp_path: Path) -> None:
    """`VORTEX_KEY_DB_PATH=/var/lib/vortex/keys.db` should not need a mkdir first."""
    store = KeyStore(tmp_path / "nested" / "deeper" / "keys.sqlite3")

    assert store.verify(store.create().token) is not None


# --- the CLI -----------------------------------------------------------------


def test_create_prints_the_token_once_and_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The token goes to stdout so it can be piped; the notice goes to stderr."""
    path = tmp_path / "keys.sqlite3"

    exit_code = cli.main(["--db", str(path), "create", "--name", "ci", "--rpm", "60"])

    captured = capsys.readouterr()
    token = captured.out.strip()
    assert exit_code == 0
    assert cli.CREATED_NOTICE in captured.err
    record = KeyStore(path).verify(token)
    assert record is not None
    assert (record.name, record.rpm) == ("ci", 60)


def test_list_shows_limits_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "keys.sqlite3"
    store = KeyStore(path)
    live = store.create(name="live", tpm=1000)
    dead = store.create(name="dead")
    store.revoke(dead.record.key_id)

    assert cli.main(["--db", str(path), "list"]) == 0

    out = capsys.readouterr().out
    assert live.record.key_id in out
    assert "tpm=1000" in out
    assert "rpm=default" in out
    assert "revoked" in out


def test_list_can_hide_revoked_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "keys.sqlite3"
    store = KeyStore(path)
    dead = store.create(name="dead")
    store.revoke(dead.record.key_id)

    assert cli.main(["--db", str(path), "list", "--active"]) == 0

    assert dead.record.key_id not in capsys.readouterr().out


def test_list_with_no_keys_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--db", str(tmp_path / "keys.sqlite3"), "list"]) == 0

    assert "No keys." in capsys.readouterr().err


def test_revoke_reports_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "keys.sqlite3"
    minted = KeyStore(path).create(name="leaked")

    exit_code = cli.main(["--db", str(path), "revoke", minted.record.key_id])

    assert exit_code == 0
    assert "leaked" in capsys.readouterr().out
    assert KeyStore(path).verify(minted.token) is None


def test_revoking_an_unknown_key_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A script that revokes a typo must not report success."""
    exit_code = cli.main(["--db", str(tmp_path / "keys.sqlite3"), "revoke", "000000000000"])

    assert exit_code == 1
    assert "No key with ID" in capsys.readouterr().err


def test_the_database_falls_back_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`vortex-keys create` with no --db uses the same path the gateway will."""
    path = tmp_path / "from-env.sqlite3"
    monkeypatch.setenv("VORTEX_KEY_DB_PATH", str(path))

    assert cli.main(["create"]) == 0

    assert KeyStore(path).verify(capsys.readouterr().out.strip()) is not None


def test_no_database_anywhere_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently creating `./keys.sqlite3` in the working directory would be worse."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VORTEX_KEY_DB_PATH", raising=False)

    exit_code = cli.main(["create"])

    assert exit_code == 2
    assert "VORTEX_KEY_DB_PATH" in capsys.readouterr().err
