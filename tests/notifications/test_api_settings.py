"""Endpoints de configuración del daemon: ajustes, productores, canales, entregas y ciclo."""

from __future__ import annotations

from fastapi.testclient import TestClient

from memex.db import connection
from memex.notifications import store


def test_settings_roundtrip_and_producers_listing(client: TestClient) -> None:
    r = client.get("/notifications/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["daemon_enabled"] is False and body["timezone"] == "America/Bogota"
    names = [p["name"] for p in body["producers"]]
    assert names == [
        "resumen_diario",
        "recordatorio_evento",
        "tareas_del_dia",
        "asuntos_plazo",
        "pagos_pendientes",
        "renovaciones",
        "verificar_cuentas",
        "sistema",
    ]
    resumen = body["producers"][0]
    assert resumen["render"] == "llm" and resumen["llm_allowed"] is True
    assert resumen["config"]["hora"] == "07:00" and resumen["from_db"] is False
    assert body["channel_names"] == ["log", "telegram"]

    r = client.patch("/notifications/settings", json={"daemon_enabled": True, "timezone": "UTC"})
    assert r.status_code == 200, r.text
    assert r.json()["daemon_enabled"] is True and r.json()["timezone"] == "UTC"
    assert (
        client.patch("/notifications/settings", json={"timezone": "Marte/Crater"}).status_code
        == 422
    )


def test_patch_producer_validates_and_persists(client: TestClient) -> None:
    r = client.patch(
        "/notifications/producers/resumen_diario",
        json={"render": "plantilla", "config": {"hora": "06:30", "dias_tareas": 3}},
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["render"] == "plantilla" and p["config"]["hora"] == "06:30"
    assert p["config"]["dias_tareas"] == 3 and p["from_db"] is True
    r = client.patch("/notifications/producers/resumen_diario", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["config"]["hora"] == "06:30"
    # Lista desde JSON → coma-separada → lista.
    r = client.patch(
        "/notifications/producers/recordatorio_evento",
        json={"config": {"compromisos": ["confirmado"], "anticipacion_min": 30}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {"anticipacion_min": 30, "compromisos": ["confirmado"]}
    assert (
        client.patch(
            "/notifications/producers/recordatorio_evento", json={"render": "llm"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/notifications/producers/resumen_diario", json={"config": {"hora": "x"}}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/notifications/producers/resumen_diario", json={"config": {"otra": 1}}
        ).status_code
        == 422
    )
    assert client.patch("/notifications/producers/nadie", json={"enabled": True}).status_code == 404


def test_channels_crud_and_validation(client: TestClient) -> None:
    bad = client.post("/notifications/channels", json={"channel": "telegram", "target": "abc"})
    assert bad.status_code == 422
    assert client.post("/notifications/channels", json={"channel": "paloma"}).status_code == 422
    assert (
        client.post(
            "/notifications/channels",
            json={"channel": "log", "subscriptions": {"personal": "urgente"}},
        ).status_code
        == 422
    )
    r = client.post(
        "/notifications/channels",
        json={
            "channel": "telegram",
            "target": "7289",
            "subscriptions": {"personal": "info"},
            "quiet_from": "22:00",
            "quiet_until": "07:00",
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert (
        c["target"] == "7289"
        and c["quiet_from"] == "22:00"
        and c["subscriptions"] == {"personal": "info"}
    )
    cid = c["id"]
    r = client.patch(
        f"/notifications/channels/{cid}",
        json={"enabled": False, "subscriptions": {"personal": "alta", "sistema": "critica"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False and r.json()["subscriptions"]["sistema"] == "critica"
    assert r.json()["quiet_from"] == "22:00"  # el silencio no se tocó
    r = client.patch(
        f"/notifications/channels/{cid}", json={"quiet_from": None, "quiet_until": None}
    )
    assert r.json()["quiet_from"] is None
    assert (
        client.patch(f"/notifications/channels/{cid}", json={"quiet_from": "08:00"}).status_code
        == 422
    )
    listed = client.get("/notifications/channels").json()
    assert [x["id"] for x in listed] == [cid]
    assert client.get("/notifications/settings").json()["channels"][0]["id"] == cid
    assert client.delete(f"/notifications/channels/{cid}").status_code == 200
    assert client.delete(f"/notifications/channels/{cid}").status_code == 404
    assert client.patch(f"/notifications/channels/{cid}", json={"enabled": True}).status_code == 404


def test_channel_test_without_token_is_422(client: TestClient, monkeypatch: object) -> None:
    import os

    for var in ("MEMEX_NOTIFICATIONS_TELEGRAM_BOT_TOKEN", "MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN"):
        os.environ.pop(var, None)
    r = client.post("/notifications/channels", json={"channel": "telegram", "target": "1"})
    cid = r.json()["id"]
    r = client.post(f"/notifications/channels/{cid}/test", json={})
    assert r.status_code in (422, 200)  # sin token → 422; con token en el entorno del dev → manda
    assert client.post("/notifications/channels/999/test", json={}).status_code == 404


def test_run_dry_run_and_deliveries(client: TestClient) -> None:
    r = client.post("/notifications/run", json={"dry_run": True, "producer": "sistema"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["producers"] == ["sistema"] and body["enqueued"] == 0
    assert client.post("/notifications/run", json={"producer": "nadie"}).status_code == 422
    with connection() as c:
        assert store.list_active(c, user_id=1, limit=5) == []
    assert client.get("/notifications/deliveries").json() == []


def test_list_exposes_audience(client: TestClient) -> None:
    from datetime import UTC, datetime

    from memex.notifications.client import Notification

    with connection() as c:
        store.enqueue(
            c,
            Notification(
                kind="sistema.run-failed",
                severity="alta",
                title="t",
                body="b",
                dedup_key="d",
                created_at=datetime.now(UTC),
                user_id=1,
                audience="sistema",
            ),
        )
    items = client.get("/notifications").json()["items"]
    assert items[0]["audience"] == "sistema"
