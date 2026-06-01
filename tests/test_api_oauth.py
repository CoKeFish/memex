"""Tests del flujo web "Conectar con Google" (endpoints + helper), mockeando Google."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from memex import google_oauth
from memex.config import settings
from memex.core.source import HealthResult
from memex.security import crypto

_MASTER_KEY = "test-master-key-de-alta-entropia-0123456789"


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memex.api.app import app

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "secret_key", _MASTER_KEY)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "google_oauth_client_secret_json", "/fake/client_secret.json")
    monkeypatch.setattr(settings, "oauth_redirect_base_url", "http://localhost:5176")
    client = TestClient(app)
    r = client.post("/auth/signup", json={"email": "g@x.io", "password": "contrasena-larga"})
    assert r.status_code == 201, r.text
    client.headers["x-user-id"] = str(r.json()["user_id"])
    return client


def _uid(client: TestClient) -> int:
    return int(client.headers["x-user-id"])


def _new_email_account(client: TestClient) -> int:
    r = client.post("/accounts", json={"alias": "gmail", "provider": "imap", "kind": "email"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


# ----- google_oauth.access_token_from_json (sin red) ------------------------ #


def test_access_token_from_json_invalid_raises() -> None:
    with pytest.raises(google_oauth.GoogleOAuthError):
        google_oauth.access_token_from_json("{ no es json")


def test_access_token_from_json_expired_without_refresh_raises() -> None:
    token = json.dumps(
        {
            "token": "x",
            "client_id": "c",
            "client_secret": "s",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": ["https://mail.google.com/"],
            "expiry": "2000-01-01T00:00:00.000000Z",
        }
    )
    with pytest.raises(google_oauth.GoogleOAuthError):
        google_oauth.access_token_from_json(token)


# ----- /accounts/{id}/oauth/google/start ------------------------------------ #


def test_start_returns_authorization_url(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        google_oauth,
        "start_authorization",
        lambda **_kw: ("https://accounts.google.com/o/oauth2/x", "verifier-x"),
    )
    aid = _new_email_account(authed)
    r = authed.get(f"/accounts/{aid}/oauth/google/start")
    assert r.status_code == 200
    assert r.json()["authorization_url"].startswith("https://accounts.google.com/")


def test_start_503_without_client_secret(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _new_email_account(authed)
    monkeypatch.setattr(settings, "google_oauth_client_secret_json", "")
    assert authed.get(f"/accounts/{aid}/oauth/google/start").status_code == 503


def test_start_cross_tenant_404(authed: TestClient) -> None:
    from memex.db import connection

    with connection() as conn:
        other = conn.execute(
            text("INSERT INTO users (email, display_name) VALUES ('o@x.io','o') RETURNING id")
        ).scalar()
        assert other is not None
        aid = conn.execute(
            text(
                "INSERT INTO accounts (user_id, alias, provider, kind) "
                "VALUES (:u,'ajena','imap','email') RETURNING id"
            ),
            {"u": int(other)},
        ).scalar()
    assert authed.get(f"/accounts/{aid}/oauth/google/start").status_code == 404


# ----- /oauth/google/callback ----------------------------------------------- #


def _sign_state(account_id: int, user_id: int) -> str:
    return crypto.sign_state(
        {"account_id": account_id, "user_id": user_id, "nonce": "n"}, now=int(time.time())
    )


def test_callback_happy_path_stores_token_and_creates_source(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memex.db import connection

    aid = _new_email_account(authed)
    token_json = '{"token":"fake-access","refresh_token":"fake-refresh","client_id":"c"}'
    monkeypatch.setattr("memex.api.routers.oauth._pop_verifier", lambda _nonce: "verifier-x")
    monkeypatch.setattr(
        "memex.api.routers.oauth._exchange_and_profile",
        lambda _state, _code, _verifier: (token_json, "neo@gmail.com"),
    )

    class _StubHealthSource:
        async def health_check(self) -> HealthResult:
            return HealthResult(status="healthy", detail="ok", checked_at=datetime.now(UTC))

    # El auto-validate post-connect instancia la source → stub para no tocar Gmail real.
    monkeypatch.setattr(
        "memex.sources.resolve", lambda _t: lambda _cfg, env=None: _StubHealthSource()
    )
    state = _sign_state(aid, _uid(authed))
    r = authed.get(
        "/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "connected=google" in r.headers["location"]

    # El token + username quedaron cifrados en el vault, y se creó la source Gmail.
    listed = authed.get("/accounts").json()
    acct = next(a for a in listed if a["id"] == aid)
    names = {s["secret_name"] for s in acct["secrets"]}
    assert {"google_oauth_token", "username"} <= names
    assert acct["health_status"] == "healthy"  # auto-validado al conectar
    with connection() as conn:
        src = (
            conn.execute(text("SELECT type, config FROM sources WHERE account_id = :a"), {"a": aid})
            .mappings()
            .first()
        )
    assert src is not None
    assert src["type"] == "imap"
    assert src["config"]["auth"] == "oauth2"
    assert src["config"]["oauth_provider"] == "google"
    # El plaintext del token NO sale por el API (solo configured/last4).
    assert "fake-access" not in json.dumps(listed)


def test_callback_bad_state_redirects_with_error(authed: TestClient) -> None:
    r = authed.get(
        "/oauth/google/callback",
        params={"code": "x", "state": "tampered.state"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "oauth_error=bad_state" in r.headers["location"]


def test_callback_user_mismatch_redirects_with_error(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _new_email_account(authed)
    # state firmado para OTRO user_id → la sesión no coincide.
    state = _sign_state(aid, _uid(authed) + 999)
    r = authed.get(
        "/oauth/google/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "oauth_error=user_mismatch" in r.headers["location"]
