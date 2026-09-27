"""Tests del router de login/discover de Telegram (`/accounts/{id}/telegram/*`).

Telethon (`TelegramClient` + `TelegramClientWrapper`) se MOCKEA — no se toca la red. Se verifica el
wiring del flujo multi-paso (request-code → submit-code [→ submit-password]), el mapeo de errores, y
que las credenciales se resuelven del vault (la cuenta tiene los secretos cifrados + una source
telegram vinculada).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

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
    r = client.post(
        "/auth/signup",
        json={"email": "tg-owner@x.io", "password": "contrasena-larga", "display_name": "Own"},
    )
    assert r.status_code == 201, r.text
    client.headers["x-user-id"] = str(r.json()["user_id"])
    return client


def _uid(client: TestClient) -> int:
    return int(client.headers["x-user-id"])


def _setup_tg(authed: TestClient) -> tuple[int, int]:
    """Crea la cuenta telegram con sus 3 credenciales en el vault + una source telegram vinculada.
    Devuelve (account_id, source_id)."""
    from memex.db import connection

    r = authed.post("/accounts", json={"alias": "tg", "provider": "telegram", "kind": "chat"})
    assert r.status_code == 201, r.text
    aid = int(r.json()["id"])
    for name, val in (("api_id", "12345"), ("api_hash", "deadbeef"), ("phone", "+34999")):
        assert (
            authed.post(
                f"/accounts/{aid}/credentials", json={"secret_name": name, "value": val}
            ).status_code
            == 200
        )
    with connection() as conn:
        sid = conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config, account_id) "
                "VALUES (:u, 'tg-src', 'telegram', CAST(:c AS JSONB), :a) RETURNING id"
            ),
            {"u": _uid(authed), "c": json.dumps({"allowed_chats": []}), "a": aid},
        ).scalar()
    assert sid is not None
    return aid, int(sid)


# ----- Fakes de Telethon ----------------------------------------------------- #


class _FakeSent:
    phone_code_hash = "phc-xyz"


class _FakeSession:
    def save(self) -> None:
        pass


class _FakeTgClient:
    """Cliente Telethon falso, controlado por atributos de clase (reset por test)."""

    authorized = False
    sign_in_error: Exception | None = None

    def __init__(self, session: Any, api_id: int, api_hash: str) -> None:
        self.session = _FakeSession()

    async def connect(self) -> None:
        return None

    async def is_user_authorized(self) -> bool:
        return _FakeTgClient.authorized

    async def send_code_request(self, phone: str) -> _FakeSent:
        return _FakeSent()

    async def sign_in(self, *args: Any, **kwargs: Any) -> None:
        if "password" in kwargs:  # paso 2FA: siempre ok en el fake
            return None
        if _FakeTgClient.sign_in_error is not None:
            raise _FakeTgClient.sign_in_error
        return None

    async def disconnect(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeTgClient.authorized = False
    _FakeTgClient.sign_in_error = None


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memex.api.routers.telegram.TelegramClient", _FakeTgClient)


# ----- request-code / submit-code -------------------------------------------- #


def test_request_code_returns_state(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    aid, _ = _setup_tg(authed)
    r = authed.post(f"/accounts/{aid}/telegram/request-code")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "code_sent"
    assert body["state"]
    assert body["phone_masked"]  # enmascarado, no vacío


def test_request_code_autocreates_source(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cuenta de Telegram con credenciales pero SIN source vinculada: el flujo la crea+vincula al
    vuelo (antes tiraba 422 y quedabas trabado sin un botón). Idempotente: no duplica."""
    from memex.db import connection

    _patch_client(monkeypatch)
    r = authed.post("/accounts", json={"alias": "tg2", "provider": "telegram", "kind": "chat"})
    aid = int(r.json()["id"])  # sin source vinculada todavía
    for name, val in (("api_id", "12345"), ("api_hash", "deadbeef"), ("phone", "+34999")):
        assert (
            authed.post(
                f"/accounts/{aid}/credentials", json={"secret_name": name, "value": val}
            ).status_code
            == 200
        )

    def _tg_sources() -> int:
        with connection() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM sources WHERE account_id = :a AND type = 'telegram'"),
                {"a": aid},
            ).scalar()
        return int(n or 0)

    resp = authed.post(f"/accounts/{aid}/telegram/request-code")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "code_sent"
    assert _tg_sources() == 1  # se creó+vinculó
    authed.post(f"/accounts/{aid}/telegram/request-code")
    assert _tg_sources() == 1  # idempotente: no duplica


def test_request_code_already_authorized(
    authed: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_client(monkeypatch)
    _FakeTgClient.authorized = True
    aid, _ = _setup_tg(authed)
    r = authed.post(f"/accounts/{aid}/telegram/request-code")
    assert r.status_code == 200
    assert r.json()["status"] == "already_authorized"


def test_submit_code_success(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    aid, _ = _setup_tg(authed)
    state = authed.post(f"/accounts/{aid}/telegram/request-code").json()["state"]
    r = authed.post(f"/accounts/{aid}/telegram/submit-code", json={"state": state, "code": "11111"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    # la cuenta queda healthy
    acc = next(a for a in authed.get("/accounts").json() if a["id"] == aid)
    assert acc["health_status"] == "healthy"


def test_submit_code_invalid(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    _FakeTgClient.sign_in_error = PhoneCodeInvalidError(request=None)
    aid, _ = _setup_tg(authed)
    state = authed.post(f"/accounts/{aid}/telegram/request-code").json()["state"]
    r = authed.post(f"/accounts/{aid}/telegram/submit-code", json={"state": state, "code": "0"})
    assert r.status_code == 400


def test_2fa_flow(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    _FakeTgClient.sign_in_error = SessionPasswordNeededError(request=None)
    aid, _ = _setup_tg(authed)
    state = authed.post(f"/accounts/{aid}/telegram/request-code").json()["state"]
    r1 = authed.post(f"/accounts/{aid}/telegram/submit-code", json={"state": state, "code": "1"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "2fa_required"
    r2 = authed.post(
        f"/accounts/{aid}/telegram/submit-password", json={"state": state, "password": "p"}
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "ok"


def test_submit_code_unknown_state(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    aid, _ = _setup_tg(authed)
    r = authed.post(f"/accounts/{aid}/telegram/submit-code", json={"state": "nope", "code": "1"})
    assert r.status_code == 410


# ----- discover -------------------------------------------------------------- #


class _FakeDialog:
    def __init__(self, did: int, name: str, *, is_channel: bool, is_group: bool) -> None:
        self.id = did
        self.name = name
        self.is_channel = is_channel
        self.is_group = is_group


class _FakeWrapper:
    """Async context manager que imita TelegramClientWrapper para discover."""

    dialogs: ClassVar[list[_FakeDialog]] = []
    raise_auth = False

    def __init__(self, cfg: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeWrapper:
        if _FakeWrapper.raise_auth:
            from memex.ingestors.telegram.client import TelegramAuthError

            raise TelegramAuthError("not authorized")
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def iter_dialogs(self) -> Any:
        for d in _FakeWrapper.dialogs:
            yield d


def test_discover_lists_chats(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeWrapper.raise_auth = False
    _FakeWrapper.dialogs = [
        _FakeDialog(-100, "Grupo", is_channel=False, is_group=True),
        _FakeDialog(-200, "Canal", is_channel=True, is_group=False),
    ]
    monkeypatch.setattr("memex.api.routers.telegram.TelegramClientWrapper", _FakeWrapper)
    aid, _ = _setup_tg(authed)
    r = authed.get(f"/accounts/{aid}/telegram/chats")
    assert r.status_code == 200, r.text
    chats = r.json()["chats"]
    assert {c["chat_id"] for c in chats} == {-100, -200}
    kinds = {c["chat_id"]: c["kind"] for c in chats}
    assert kinds[-100] == "group" and kinds[-200] == "channel"


def test_discover_not_authorized_422(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeWrapper.raise_auth = True
    monkeypatch.setattr("memex.api.routers.telegram.TelegramClientWrapper", _FakeWrapper)
    aid, _ = _setup_tg(authed)
    r = authed.get(f"/accounts/{aid}/telegram/chats")
    assert r.status_code == 422
