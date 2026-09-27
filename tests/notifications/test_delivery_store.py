"""Canales y entregas (0097): alta/cambios, qué pares (aviso, canal) faltan entregar, reintentos."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection

from memex.notifications import store
from memex.notifications.client import Notification


def _notif(
    *,
    dedup_key: str,
    audience: str = "personal",
    severity: str = "info",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Notification:
    return Notification(
        kind="tareas.vence_hoy",
        severity=severity,
        title=f"t-{dedup_key}",
        body="cuerpo",
        dedup_key=dedup_key,
        created_at=created_at or (datetime.now(UTC) + timedelta(seconds=1)),
        user_id=1,
        audience=audience,
        expires_at=expires_at,
    )


def _channel(conn: Connection, **kw: Any) -> int:
    params: dict[str, Any] = {
        "user_id": 1,
        "channel": "fake",
        "target": "42",
        "subscriptions": {"personal": "info", "sistema": "critica"},
    }
    params.update(kw)
    return store.upsert_channel(conn, **params)


def test_channel_upsert_update_remove(conn: Connection) -> None:
    cid = _channel(conn, quiet_from=time(22, 0), quiet_until=time(7, 0))
    again = _channel(conn, subscriptions={"personal": "alta"})
    assert again == cid  # mismo (user, channel, target) → misma fila
    rows = store.list_channels(conn, user_id=1)
    assert len(rows) == 1
    assert rows[0]["subscriptions"] == {"personal": "alta"}
    assert rows[0]["quiet_from"] is None  # el reemplazo pisa el silencio
    assert store.update_channel(conn, user_id=1, channel_id=cid, enabled=False) is True
    assert store.list_channels(conn, user_id=1, enabled_only=True) == []
    assert store.update_channel(conn, user_id=1, channel_id=cid, quiet=(time(1), time(2))) is True
    got = store.get_channel(conn, user_id=1, channel_id=cid)
    assert got is not None and got["quiet_from"] == time(1) and got["enabled"] is False
    assert store.update_channel(conn, user_id=1, channel_id=999, enabled=True) is False
    assert store.remove_channel(conn, user_id=1, channel_id=cid) is True
    assert store.list_channels(conn, user_id=1) == []


def test_pending_only_active_unread_subscribed_and_after_channel(conn: Connection) -> None:
    cid = _channel(conn, subscriptions={"personal": "info"})  # no suscribe `sistema`
    ok = store.enqueue(conn, _notif(dedup_key="ok"))
    store.enqueue(conn, _notif(dedup_key="viejo", created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    store.enqueue(conn, _notif(dedup_key="sistema", audience="sistema", severity="critica"))
    leido = store.enqueue(conn, _notif(dedup_key="leido"))
    store.mark_read(conn, notification_id=leido, user_id=1)
    desc = store.enqueue(conn, _notif(dedup_key="descartado"))
    store.dismiss(conn, notification_id=desc, user_id=1)
    store.enqueue(
        conn,
        _notif(dedup_key="vencido", expires_at=datetime.now(UTC) - timedelta(minutes=1)),
    )
    rows = store.pending_deliveries(conn, user_id=1)
    assert [(r["notification_id"], r["channel_id"]) for r in rows] == [(ok, cid)]
    assert rows[0]["channel"] == "fake" and rows[0]["target"] == "42"
    assert rows[0]["attempts"] == 0


def test_pending_skips_disabled_channels(conn: Connection) -> None:
    cid = _channel(conn)
    store.enqueue(conn, _notif(dedup_key="a"))
    store.update_channel(conn, user_id=1, channel_id=cid, enabled=False)
    assert store.pending_deliveries(conn, user_id=1) == []


def test_record_delivery_sent_once_failed_retries_with_cap(conn: Connection) -> None:
    cid = _channel(conn)
    nid = store.enqueue(conn, _notif(dedup_key="a"))
    store.record_delivery(
        conn, notification_id=nid, channel_id=cid, status="fallida", error="red caída"
    )
    # Dentro de la ventana de reintento no se reintenta; pasada la ventana sí.
    assert store.pending_deliveries(conn, user_id=1, retry_after_s=300) == []
    rows = store.pending_deliveries(conn, user_id=1, retry_after_s=-1)
    assert len(rows) == 1 and rows[0]["attempts"] == 1
    store.record_delivery(conn, notification_id=nid, channel_id=cid, status="fallida", error="x")
    store.record_delivery(conn, notification_id=nid, channel_id=cid, status="fallida", error="y")
    # 3 intentos fallidos → tope (max_attempts=3): ya no está pendiente.
    assert store.pending_deliveries(conn, user_id=1, retry_after_s=-1, max_attempts=3) == []
    store.record_delivery(
        conn, notification_id=nid, channel_id=cid, status="enviada", external_ref="m1"
    )
    assert store.pending_deliveries(conn, user_id=1, retry_after_s=-1, max_attempts=99) == []
    d = store.list_deliveries(conn, user_id=1)
    assert len(d) == 1
    assert d[0]["status"] == "enviada" and d[0]["attempts"] == 4 and d[0]["external_ref"] == "m1"
    assert d[0]["sent_at"] is not None and d[0]["title"] == "t-a"


def test_exists_and_audience_roundtrip(conn: Connection) -> None:
    assert store.exists(conn, user_id=1, dedup_key="x") is False
    store.enqueue(conn, _notif(dedup_key="x", audience="sistema"))
    assert store.exists(conn, user_id=1, dedup_key="x") is True
    rows = store.list_active(conn, user_id=1, limit=10)
    assert rows[0]["audience"] == "sistema"
