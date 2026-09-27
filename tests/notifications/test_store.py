"""Cola de notificaciones (store): encolar idempotente + ciclo de vida + purga (Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from memex.notifications import store
from memex.notifications.client import Notification


def _notif(
    *,
    dedup_key: str = "transport:1:42:leave_now",
    title: str = "Salí ya",
    body: str = "Salí antes de las 14:40.",
    severity: str = "alta",
    payload: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Notification:
    return Notification(
        kind="transport.leave_by",
        severity=severity,
        title=title,
        body=body,
        dedup_key=dedup_key,
        created_at=datetime(2026, 6, 17, 14, 10, tzinfo=UTC),
        user_id=1,
        payload=payload if payload is not None else {"event_id": 42},
        deep_link="/calendario",
        expires_at=expires_at,
    )


def test_enqueue_inserts_one_active(conn: Connection) -> None:
    nid = store.enqueue(conn, _notif())
    rows = store.list_active(conn, user_id=1, limit=50)
    assert [r["id"] for r in rows] == [nid]
    assert rows[0]["kind"] == "transport.leave_by"
    assert rows[0]["deep_link"] == "/calendario"
    assert rows[0]["payload"] == {"event_id": 42}
    assert store.count_unread(conn, user_id=1) == 1


def test_enqueue_collapses_by_dedup_key(conn: Connection) -> None:
    first = store.enqueue(conn, _notif(title="v1"))
    again = store.enqueue(conn, _notif(title="v2", body="nuevo", severity="critica"))
    assert again == first  # misma fila (colapso por dedup_key)
    rows = store.list_active(conn, user_id=1, limit=50)
    assert len(rows) == 1
    assert rows[0]["title"] == "v2"
    assert rows[0]["body"] == "nuevo"
    assert rows[0]["severity"] == "critica"


def test_mark_read_clears_unread_but_keeps_in_queue(conn: Connection) -> None:
    nid = store.enqueue(conn, _notif())
    assert store.mark_read(conn, notification_id=nid, user_id=1) is True
    assert store.count_unread(conn, user_id=1) == 0
    rows = store.list_active(conn, user_id=1, limit=50)  # leído != descartado: sigue en la cola
    assert len(rows) == 1
    assert rows[0]["read_at"] is not None


def test_mark_read_unknown_returns_false(conn: Connection) -> None:
    assert store.mark_read(conn, notification_id=999999, user_id=1) is False


def test_dismiss_removes_from_active(conn: Connection) -> None:
    nid = store.enqueue(conn, _notif())
    assert store.dismiss(conn, notification_id=nid, user_id=1) is True
    assert store.list_active(conn, user_id=1, limit=50) == []
    assert store.count_unread(conn, user_id=1) == 0


def test_collapse_preserves_read_and_dismiss(conn: Connection) -> None:
    nid = store.enqueue(conn, _notif())
    store.mark_read(conn, notification_id=nid, user_id=1)
    store.dismiss(conn, notification_id=nid, user_id=1)
    again = store.enqueue(conn, _notif(title="re-emitido"))  # misma dedup_key
    assert again == nid
    assert store.list_active(conn, user_id=1, limit=50) == []  # sigue descartado (pegajoso)


def test_expired_hidden_and_purgeable(conn: Connection) -> None:
    store.enqueue(conn, _notif(expires_at=datetime(2020, 1, 1, tzinfo=UTC)))
    assert store.list_active(conn, user_id=1, limit=50) == []  # vencido: oculto en lectura
    assert store.count_unread(conn, user_id=1) == 0
    assert store.purge_expired(conn) == 1
    assert store.purge_expired(conn) == 0  # ya borrado


def test_mark_all_read(conn: Connection) -> None:
    store.enqueue(conn, _notif(dedup_key="a"))
    store.enqueue(conn, _notif(dedup_key="b"))
    assert store.count_unread(conn, user_id=1) == 2
    assert store.mark_all_read(conn, user_id=1) == 2
    assert store.count_unread(conn, user_id=1) == 0
