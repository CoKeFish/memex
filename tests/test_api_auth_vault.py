"""Tests del flujo de auth multi-usuario (signup/login/logout/me/change-password) con sesión cookie.

Usa `auth_enforced=True` para que la cookie de sesión sea lo que identifica al usuario.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

_MASTER_KEY = "test-master-key-de-alta-entropia-0123456789"


@pytest.fixture
def authed(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "secret_key", _MASTER_KEY)
    monkeypatch.setattr(settings, "cookie_secure", False)
    return TestClient(app)


def _signup(client: TestClient, email: str = "neo@matrix.io", pw: str = "trinity-1999") -> Any:
    return client.post("/auth/signup", json={"email": email, "password": pw, "display_name": "Neo"})


def test_signup_sets_session_and_me_works(authed: TestClient) -> None:
    r = _signup(authed)
    assert r.status_code == 201, r.text
    uid = r.json()["user_id"]
    assert authed.cookies.get("memex_session")
    me = authed.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == uid
    assert me.json()["email"] == "neo@matrix.io"


def test_session_cookie_authenticates_protected_endpoint(authed: TestClient) -> None:
    _signup(authed)
    # /inbox exige auth cuando enforced; la cookie debe alcanzar.
    assert authed.get("/inbox").status_code == 200


def test_logout_clears_session(authed: TestClient) -> None:
    _signup(authed)
    assert authed.post("/auth/logout").status_code == 204
    # Sin cookie válida ni bearer → 401.
    assert authed.get("/auth/me").status_code == 401


def test_login_after_logout(authed: TestClient) -> None:
    _signup(authed, email="user@x.io", pw="contrasena-larga")
    authed.post("/auth/logout")
    r = authed.post("/auth/login", json={"email": "user@x.io", "password": "contrasena-larga"})
    assert r.status_code == 200
    assert authed.get("/auth/me").status_code == 200


def test_login_wrong_password_is_401(authed: TestClient) -> None:
    _signup(authed, email="user@x.io", pw="contrasena-larga")
    authed.post("/auth/logout")
    r = authed.post("/auth/login", json={"email": "user@x.io", "password": "mala"})
    assert r.status_code == 401


def test_login_unknown_email_is_401(authed: TestClient) -> None:
    r = authed.post("/auth/login", json={"email": "ghost@x.io", "password": "whatever-long"})
    assert r.status_code == 401


def test_signup_duplicate_email_is_409(authed: TestClient) -> None:
    _signup(authed, email="dup@x.io")
    r = _signup(authed, email="dup@x.io")
    assert r.status_code == 409


def test_signup_short_password_is_422(authed: TestClient) -> None:
    r = authed.post("/auth/signup", json={"email": "a@b.io", "password": "short"})
    assert r.status_code == 422


def test_change_password_rotates_credential(authed: TestClient) -> None:
    _signup(authed, email="rot@x.io", pw="vieja-contrasena")
    r = authed.post(
        "/auth/change-password",
        json={"current_password": "vieja-contrasena", "new_password": "nueva-contrasena"},
    )
    assert r.status_code == 204
    authed.post("/auth/logout")
    assert (
        authed.post(
            "/auth/login", json={"email": "rot@x.io", "password": "nueva-contrasena"}
        ).status_code
        == 200
    )
    authed.post("/auth/logout")
    assert (
        authed.post(
            "/auth/login", json={"email": "rot@x.io", "password": "vieja-contrasena"}
        ).status_code
        == 401
    )


def test_change_password_wrong_current_is_403(authed: TestClient) -> None:
    _signup(authed, email="cp@x.io", pw="actual-contrasena")
    r = authed.post(
        "/auth/change-password",
        json={"current_password": "equivocada", "new_password": "nueva-contrasena"},
    )
    assert r.status_code == 403


def test_signup_without_master_key_is_503(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memex.config import settings

    monkeypatch.setattr(settings, "secret_key", "")
    r = _signup(authed, email="nokey@x.io")
    assert r.status_code == 503


def test_two_users_have_distinct_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "secret_key", _MASTER_KEY)
    monkeypatch.setattr(settings, "cookie_secure", False)

    a = TestClient(app)
    b = TestClient(app)
    ua = _signup(a, email="a@x.io").json()["user_id"]
    ub = _signup(b, email="b@x.io").json()["user_id"]
    assert ua != ub
    assert a.get("/auth/me").json()["user_id"] == ua
    assert b.get("/auth/me").json()["user_id"] == ub
