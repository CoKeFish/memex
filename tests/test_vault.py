"""Tests de `memex.security.vault` — DEK por-usuario + secretos cifrados (contra la DB de test)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.config import settings
from memex.security import crypto, vault


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "test-master-key-de-alta-entropia-0123456789")


def _make_account(conn: Any, *, user_id: int = 1, alias: str = "mi-gmail") -> int:
    aid = conn.execute(
        text(
            "INSERT INTO accounts (user_id, alias, provider, kind) "
            "VALUES (:u, :a, 'imap', 'email') RETURNING id"
        ),
        {"u": user_id, "a": alias},
    ).scalar()
    assert aid is not None
    return int(aid)


def test_provision_and_get_dek_round_trip(conn: Any) -> None:
    vault.provision_user(conn, 1, "s3cret-pass")
    dek = vault.get_user_dek(conn, 1)
    assert len(dek) == 32
    assert vault.get_user_dek(conn, 1) == dek  # estable entre llamadas


def test_get_dek_for_unprovisioned_user_raises(conn: Any) -> None:
    with pytest.raises(vault.UserVaultMissingError):
        vault.get_user_dek(conn, 1)


def test_verify_password(conn: Any) -> None:
    vault.provision_user(conn, 1, "correcta")
    assert vault.verify_user_password(conn, 1, "correcta") is True
    assert vault.verify_user_password(conn, 1, "incorrecta") is False


def test_change_password_keeps_dek(conn: Any) -> None:
    vault.provision_user(conn, 1, "vieja")
    dek_before = vault.get_user_dek(conn, 1)
    vault.change_user_password(conn, 1, "nueva")
    # El DEK NO cambia → los secretos siguen descifrables (reset lossless).
    assert vault.get_user_dek(conn, 1) == dek_before
    assert vault.verify_user_password(conn, 1, "nueva") is True
    assert vault.verify_user_password(conn, 1, "vieja") is False


def test_set_and_get_secret_round_trip(conn: Any) -> None:
    vault.provision_user(conn, 1, "pass")
    aid = _make_account(conn)
    last4 = vault.set_secret(conn, aid, "password", "hunter2-de-gmail")
    assert last4 == "mail"
    secrets = vault.get_account_secrets(conn, aid)
    assert secrets == {"password": "hunter2-de-gmail"}


def test_set_secret_upsert_overwrites(conn: Any) -> None:
    vault.provision_user(conn, 1, "pass")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "uno")
    vault.set_secret(conn, aid, "password", "dos-mas-largo")
    assert vault.get_account_secrets(conn, aid) == {"password": "dos-mas-largo"}


def test_ciphertext_is_not_plaintext_in_db(conn: Any) -> None:
    vault.provision_user(conn, 1, "pass")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "PLAINTEXT-SECRETO")
    raw = conn.execute(
        text("SELECT ciphertext FROM account_secrets WHERE account_id = :a"), {"a": aid}
    ).scalar()
    assert b"PLAINTEXT-SECRETO" not in bytes(raw)


def test_list_secret_status_masks(conn: Any) -> None:
    vault.provision_user(conn, 1, "pass")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "abcdef")
    status = vault.list_secret_status(conn, aid)
    assert status == [{"secret_name": "password", "configured": True, "last4": "cdef"}]


def test_delete_secret(conn: Any) -> None:
    vault.provision_user(conn, 1, "pass")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "x")
    assert vault.delete_secret(conn, aid, "password") is True
    assert vault.delete_secret(conn, aid, "password") is False
    assert vault.get_account_secrets(conn, aid) == {}


def test_secrets_isolated_per_user_dek(conn: Any) -> None:
    # Dos usuarios con DEKs distintos: el secreto de uno no se descifra con el del otro.
    vault.provision_user(conn, 1, "pass1")
    uid2 = conn.execute(
        text("INSERT INTO users (email, display_name) VALUES ('u2@x', 'u2') RETURNING id")
    ).scalar()
    vault.provision_user(conn, int(uid2), "pass2")
    assert vault.get_user_dek(conn, 1) != vault.get_user_dek(conn, int(uid2))


def test_master_key_missing_blocks_vault(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "")
    with pytest.raises(crypto.MasterKeyMissingError):
        vault.provision_user(conn, 1, "pass")
