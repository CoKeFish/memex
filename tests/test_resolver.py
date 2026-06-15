"""Tests de `memex.sources.resolver.build_resolved_env` (contra la DB de test)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.config import settings
from memex.security import vault
from memex.sources.resolver import build_resolved_env

_TARGET = "MEMEX_TEST_INJECT_TARGET"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "test-master-key-de-alta-entropia-0123456789")


def _make_account(conn: Any, *, provider: str = "imap", kind: str = "email") -> int:
    aid = conn.execute(
        text(
            "INSERT INTO accounts (user_id, alias, provider, kind) "
            "VALUES (1, :a, :p, :k) RETURNING id"
        ),
        {"a": f"{provider}-acct", "p": provider, "k": kind},
    ).scalar()
    assert aid is not None
    return int(aid)


def test_no_account_strips_creds_keeps_rest(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sin cuenta/vault: las credenciales se QUITAN del env aunque vengan del .env/host.
    # Lo no-credencial pasa intacto. El `.env` ya no es fuente de credenciales.
    monkeypatch.setenv("SOME_PASSTHROUGH", "yes")
    monkeypatch.setenv("MEMEX_TG_API_ID", "from-dot-env")
    env = build_resolved_env(conn, user_id=1, source_type="telegram", cfg={}, account_id=None)
    assert env["SOME_PASSTHROUGH"] == "yes"  # no-credencial: pasa
    assert "MEMEX_TG_API_ID" not in env  # credencial: removida (no se hereda del .env)


def test_imap_injects_under_cfg_env_name(conn: Any) -> None:
    vault.provision_user(conn, 1, "p")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "imap-secret-val")
    cfg = {"username_env": "MEMEX_GMAIL_USER", "password_env": _TARGET}
    env = build_resolved_env(conn, user_id=1, source_type="imap", cfg=cfg, account_id=aid)
    assert env[_TARGET] == "imap-secret-val"


def test_telegram_injects_under_default_env_name(conn: Any) -> None:
    vault.provision_user(conn, 1, "p")
    aid = _make_account(conn, provider="telegram", kind="chat")
    vault.set_secret(conn, aid, "api_id", "987654")
    # cfg sin api_id_env → el resolver usa el default MEMEX_TG_API_ID (igual que el ingestor).
    env = build_resolved_env(conn, user_id=1, source_type="telegram", cfg={}, account_id=aid)
    assert env["MEMEX_TG_API_ID"] == "987654"


def test_imap_injects_google_oauth_token(conn: Any) -> None:
    vault.provision_user(conn, 1, "p")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "google_oauth_token", '{"token":"abc","refresh_token":"r"}')
    cfg = {"oauth_token_env": "MEMEX_OAUTH_TOKEN_ACCT_1"}
    env = build_resolved_env(conn, user_id=1, source_type="imap", cfg=cfg, account_id=aid)
    assert env["MEMEX_OAUTH_TOKEN_ACCT_1"] == '{"token":"abc","refresh_token":"r"}'


def test_account_without_vault_secret_strips_env_cred(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cuenta provisionada pero SIN el secreto en el vault: la env var de credencial se quita igual
    # (no se hereda del .env/host).
    monkeypatch.setenv(_TARGET, "from-dot-env")
    vault.provision_user(conn, 1, "p")
    aid = _make_account(conn)  # sin secretos
    env = build_resolved_env(
        conn, user_id=1, source_type="imap", cfg={"password_env": _TARGET}, account_id=aid
    )
    assert _TARGET not in env


def test_account_without_vault_strips_env_cred(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cuenta cuyo dueño nunca provisionó el vault: tampoco se cae al .env; la credencial se quita.
    monkeypatch.setenv(_TARGET, "from-dot-env")
    aid = _make_account(conn)
    env = build_resolved_env(
        conn, user_id=1, source_type="imap", cfg={"password_env": _TARGET}, account_id=aid
    )
    assert _TARGET not in env


def test_master_key_missing_strips_env_cred(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sin master key no se puede descifrar el vault → la credencial igual se quita (no cae al .env).
    monkeypatch.setenv(_TARGET, "from-dot-env")
    vault.provision_user(conn, 1, "p")
    aid = _make_account(conn)
    vault.set_secret(conn, aid, "password", "s")
    monkeypatch.setattr(settings, "secret_key", "")  # quita la master key
    env = build_resolved_env(
        conn, user_id=1, source_type="imap", cfg={"password_env": _TARGET}, account_id=aid
    )
    assert _TARGET not in env
