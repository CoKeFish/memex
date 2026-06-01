"""Tests del router de cuentas + credenciales (cliente autenticado con vault provisionado)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

_MASTER_KEY = "test-master-key-de-alta-entropia-0123456789"


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "secret_key", _MASTER_KEY)
    monkeypatch.setattr(settings, "cookie_secure", False)
    client = TestClient(app)
    # signup → crea usuario + vault + sesión (cookie en el jar del client).
    r = client.post(
        "/auth/signup",
        json={"email": "owner@x.io", "password": "contrasena-larga", "display_name": "Own"},
    )
    assert r.status_code == 201, r.text
    client.headers["x-user-id"] = str(r.json()["user_id"])  # solo para que el test lo lea
    return client


def _uid(client: TestClient) -> int:
    return int(client.headers["x-user-id"])


def _new_account(
    client: TestClient, alias: str, *, provider: str = "imap", kind: str = "email"
) -> int:
    r = client.post("/accounts", json={"alias": alias, "provider": provider, "kind": kind})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def _new_source(client: TestClient, name: str) -> int:
    from memex.db import connection

    with connection() as conn:
        sid = conn.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, 'imap') RETURNING id"),
            {"u": _uid(client), "n": name},
        ).scalar()
    assert sid is not None
    return int(sid)


def _set_cred(client: TestClient, account_id: int, name: str, value: str) -> Any:
    return client.post(
        f"/accounts/{account_id}/credentials", json={"secret_name": name, "value": value}
    )


def test_create_list_account(authed: TestClient) -> None:
    r = authed.post("/accounts", json={"alias": "mi-gmail", "provider": "imap", "kind": "email"})
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["alias"] == "mi-gmail"
    assert acc["secrets"] == []
    assert acc["health_status"] == "unknown"
    listed = authed.get("/accounts").json()
    assert any(a["id"] == acc["id"] for a in listed)


def test_duplicate_alias_is_409(authed: TestClient) -> None:
    _new_account(authed, "dup")
    r = authed.post("/accounts", json={"alias": "dup", "provider": "imap", "kind": "email"})
    assert r.status_code == 409


def test_set_credential_masks_value(authed: TestClient) -> None:
    aid = _new_account(authed, "gmail")
    r = _set_cred(authed, aid, "password", "hunter2-secreto")
    assert r.status_code == 200
    assert r.json() == {"secret_name": "password", "configured": True, "last4": "reto"}
    # La lista refleja configured + last4, nunca el valor.
    acc = next(a for a in authed.get("/accounts").json() if a["id"] == aid)
    assert acc["secrets"] == [{"secret_name": "password", "configured": True, "last4": "reto"}]


def test_delete_credential(authed: TestClient) -> None:
    aid = _new_account(authed, "g")
    _set_cred(authed, aid, "password", "abcd1234")
    assert authed.delete(f"/accounts/{aid}/credentials/password").status_code == 204
    acc = next(a for a in authed.get("/accounts").json() if a["id"] == aid)
    assert acc["secrets"] == []


def test_patch_account_alias(authed: TestClient) -> None:
    aid = _new_account(authed, "viejo")
    r = authed.patch(f"/accounts/{aid}", json={"alias": "nuevo"})
    assert r.status_code == 200
    assert r.json()["alias"] == "nuevo"


def test_link_source_to_account_returns_alias(authed: TestClient) -> None:
    aid = _new_account(authed, "cuenta-x")
    sid = _new_source(authed, "src-x")
    r = authed.patch(f"/sources/{sid}", json={"account_id": aid})
    assert r.status_code == 200
    assert r.json()["account_id"] == aid
    assert r.json()["account_alias"] == "cuenta-x"


def test_delete_account_blocked_by_linked_source(authed: TestClient) -> None:
    aid = _new_account(authed, "blk")
    sid = _new_source(authed, "blk")
    authed.patch(f"/sources/{sid}", json={"account_id": aid})
    assert authed.delete(f"/accounts/{aid}").status_code == 409
    # cascade desvincula y borra.
    assert authed.delete(f"/accounts/{aid}?cascade=true").status_code == 204


def test_health_check_requires_linked_source(authed: TestClient) -> None:
    aid = _new_account(authed, "hc")
    assert authed.post(f"/accounts/{aid}/health-check").status_code == 422


def test_health_check_runs_and_persists(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memex.core.source import HealthResult

    class _StubSource:
        async def health_check(self) -> HealthResult:
            return HealthResult(status="healthy", detail="login ok", checked_at=datetime.now(UTC))

    monkeypatch.setattr("memex.sources.resolve", lambda _t: lambda _cfg, env=None: _StubSource())

    aid = _new_account(authed, "hcok")
    sid = _new_source(authed, "hcok")
    authed.patch(f"/sources/{sid}", json={"account_id": aid})

    r = authed.post(f"/accounts/{aid}/health-check")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    acc = next(a for a in authed.get("/accounts").json() if a["id"] == aid)
    assert acc["health_status"] == "healthy"


def test_cross_tenant_account_is_404(authed: TestClient) -> None:
    from memex.db import connection

    # Cuenta de OTRO usuario → 404 al accederla.
    with connection() as conn:
        other = conn.execute(
            text("INSERT INTO users (email, display_name) VALUES ('z@z.io', 'z') RETURNING id")
        ).scalar()
        assert other is not None
        aid = conn.execute(
            text(
                "INSERT INTO accounts (user_id, alias, provider, kind) "
                "VALUES (:u, 'ajena', 'imap', 'email') RETURNING id"
            ),
            {"u": int(other)},
        ).scalar()
    assert aid is not None
    assert authed.patch(f"/accounts/{aid}", json={"alias": "x"}).status_code == 404
    assert _set_cred(authed, int(aid), "password", "x").status_code == 404
