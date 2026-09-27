"""Token de calendario desde el VAULT de la cuenta del dashboard (0100), sin red.

Cubre: la cuenta ligada (`account_id`) resuelve el token del vault (y no toca el archivo legacy),
una cuenta sin Google conectado falla con un error de proveedor claro, y la cuenta sin `account_id`
sigue por la vía del archivo.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex import google_oauth
from memex.config import settings
from memex.db import connection
from memex.modules.calendar import sync
from memex.modules.calendar.providers import oauth
from memex.modules.calendar.providers.base import CalendarProviderError
from memex.security import vault


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "test-master-key-de-alta-entropia-0123456789")


def _dashboard_account(conn: Any, *, token_json: str | None) -> int:
    vault.provision_user(conn, 1, "p")
    aid = int(
        conn.execute(
            text(
                "INSERT INTO accounts (user_id, alias, provider, kind) "
                "VALUES (1, 'Gmail', 'google', 'email') RETURNING id"
            )
        ).scalar_one()
    )
    if token_json is not None:
        vault.set_secret(conn, aid, oauth.TOKEN_SECRET_NAME, token_json)
    return aid


def _calendar_account(
    conn: Any, *, account_id: int | None, token_path_env: str | None, label: str
) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_provider_accounts
                  (user_id, provider, account_label, token_path_env, account_id)
                VALUES (1, 'google', :label, :env, :acct)
                RETURNING id
                """
            ),
            {"env": token_path_env, "acct": account_id, "label": label},
        ).scalar_one()
    )


def test_vault_token_is_decrypted_and_refreshed_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_access(token_json: str, scopes: list[str] | None = None) -> str:
        seen.append(token_json)
        return "ACCESS-TOKEN"

    monkeypatch.setattr(google_oauth, "access_token_from_json", fake_access)
    with connection() as conn:
        aid = _dashboard_account(conn, token_json='{"token": "abc", "refresh_token": "r"}')
        assert oauth.access_token_from_vault(conn, aid) == "ACCESS-TOKEN"
    assert seen == ['{"token": "abc", "refresh_token": "r"}']  # el JSON del vault, descifrado


def test_account_without_google_connected_raises_provider_error() -> None:
    with connection() as conn:
        aid = _dashboard_account(conn, token_json=None)
        with pytest.raises(CalendarProviderError, match="conectá Google en /cuenta"):
            oauth.access_token_from_vault(conn, aid)


def test_linked_calendar_account_uses_vault_not_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con `account_id` el token sale del vault aunque haya `token_path_env`; si no, del archivo."""
    calls: list[str] = []

    def from_vault(conn: Any, aid: int) -> str:
        calls.append(f"vault:{aid}")
        return "V"

    def from_file(provider: str, *, token_path: str) -> str:
        calls.append("file")
        return "F"

    monkeypatch.setattr(oauth, "access_token_from_vault", from_vault)
    monkeypatch.setattr(oauth, "access_token", from_file)
    with connection() as conn:
        dash = _dashboard_account(conn, token_json='{"token": "abc", "refresh_token": "r"}')
        linked = _calendar_account(conn, account_id=dash, token_path_env=None, label="vault")
        legacy = _calendar_account(
            conn, account_id=None, token_path_env="X_TOKEN_PATH", label="archivo"
        )
        a_linked = sync._load_account(conn, 1, linked)
        a_legacy = sync._load_account(conn, 1, legacy)
    assert a_linked is not None and a_linked.account_id == dash
    assert a_linked.token_path_env is None
    assert sync._access_token(a_linked) == "V"
    assert a_legacy is not None and a_legacy.account_id is None
    assert sync._access_token(a_legacy) == "F"
    assert calls == [f"vault:{dash}", "file"]
