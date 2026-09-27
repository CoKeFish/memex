"""«Validar» para una cuenta de WhatsApp: salud por estado del vínculo, no por ingestor."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.whatsapp import health, store
from memex.whatsapp.runtime import runtime


def _new_account() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO accounts (user_id, alias, provider, kind, metadata) "
                    "VALUES (1, :alias, 'whatsapp', 'chat', '{}'::jsonb) RETURNING id"
                ),
                {"alias": f"wa-h-{uuid4().hex[:6]}"},
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("status", "connected", "expected", "code"),
    [
        ("vinculado", True, "healthy", None),
        ("vinculado", False, "degraded", None),
        ("desconectado", False, "degraded", None),
        ("codigo", True, "unhealthy", health.HEALTH_CODE_LINK_REQUIRED),
        ("cerrado", False, "unhealthy", health.HEALTH_CODE_LINK_REQUIRED),
        ("sin_vincular", False, "unhealthy", health.HEALTH_CODE_LINK_REQUIRED),
        ("baneado", False, "unhealthy", None),
    ],
)
def test_assess_maps_link_state(
    status: str, connected: bool, expected: str, code: str | None
) -> None:
    r = health.assess(status, connected, "code=403")
    assert (r.status, r.code) == (expected, code)
    assert r.detail


def test_registry_exposes_device_health_only_for_push_types() -> None:
    from memex import sources

    assert sources.resolve_device_health("whatsapp") is health.health_check
    assert sources.resolve_device_health("imap") is None


@pytest.fixture
def wa_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", False)
    monkeypatch.setattr(settings, "api_token", "")
    return TestClient(app)


def test_validar_whatsapp_account_end_to_end(
    wa_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antes: 422 «tipo de source sin ingestor server-side». Ahora el health-check persiste el
    estado del vínculo en la cuenta, como con cualquier otra fuente."""
    aid = _new_account()
    with connection() as c:
        store.ensure_source(c, 1, aid)

    monkeypatch.setattr(runtime, "is_connected", lambda account_id: False)
    r = wa_client.post(f"/accounts/{aid}/health-check")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["code"]) == ("unhealthy", health.HEALTH_CODE_LINK_REQUIRED)

    with connection() as c:
        store.upsert_link(c, 1, aid, status="vinculado", phone="573001112233")
    monkeypatch.setattr(runtime, "is_connected", lambda account_id: True)
    r = wa_client.post(f"/accounts/{aid}/health-check")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    acc = next(a for a in wa_client.get("/accounts").json() if a["id"] == aid)
    assert (acc["health_status"], acc["health_code"]) == ("healthy", None)


def test_health_check_from_another_process_reads_the_persisted_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El daemon de notificaciones no tiene el cliente (vive en el API): con el vínculo persistido
    en `vinculado` la cuenta está sana; `desconectado` (lo escribe el runtime al caerse) → degraded.
    Si el proceso SÍ es dueño del cliente, manda la conexión viva."""
    import asyncio

    account_id = _new_account()
    with connection() as c:
        store.upsert_link(c, 1, account_id, status="vinculado", jid="573001@s.whatsapp.net")
    monkeypatch.setattr(runtime, "owns", lambda _aid: False)
    monkeypatch.setattr(runtime, "is_connected", lambda _aid: False)
    assert asyncio.run(health.health_check(account_id)).status == "healthy"
    with connection() as c:
        store.set_link_status(c, account_id, "desconectado")
    assert asyncio.run(health.health_check(account_id)).status == "degraded"
    with connection() as c:
        store.set_link_status(c, account_id, "vinculado")
    # dueño del cliente pero sin conexión viva ahora → degraded (reconecta solo)
    monkeypatch.setattr(runtime, "owns", lambda _aid: True)
    assert asyncio.run(health.health_check(account_id)).status == "degraded"
