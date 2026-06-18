"""Endpoints de la cola de notificaciones (GET + read / dismiss / read-all)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from memex.db import connection
from memex.notifications import store
from memex.notifications.client import Notification


def _notif(
    *, dedup_key: str = "d1", title: str = "t", expires_at: datetime | None = None
) -> Notification:
    return Notification(
        kind="transport.leave_by",
        severity="alta",
        title=title,
        body="cuerpo",
        dedup_key=dedup_key,
        created_at=datetime(2026, 6, 17, 14, 10, tzinfo=UTC),
        user_id=1,
        payload={"event_id": 42},
        deep_link="/calendario",
        expires_at=expires_at,
    )


def _seed(n: Notification) -> int:
    # Conexión PROPIA que commitea, para que el TestClient (abre otra conexión) lo vea.
    with connection() as c:
        return store.enqueue(c, n)


def test_list_returns_active_newest_first_with_unread(client: TestClient) -> None:
    _seed(_notif(dedup_key="a", title="A"))
    _seed(_notif(dedup_key="b", title="B"))
    r = client.get("/notifications")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unread"] == 2
    assert [i["title"] for i in body["items"]] == ["B", "A"]  # newest-first (id DESC)
    assert body["next_cursor"] is None
    assert body["items"][0]["deep_link"] == "/calendario"


def test_mark_read_drops_unread(client: TestClient) -> None:
    nid = _seed(_notif())
    assert client.post(f"/notifications/{nid}/read").status_code == 200
    body = client.get("/notifications").json()
    assert body["unread"] == 0
    assert body["items"][0]["read_at"] is not None


def test_read_unknown_is_404(client: TestClient) -> None:
    assert client.post("/notifications/999999/read").status_code == 404


def test_dismiss_hides_from_list(client: TestClient) -> None:
    nid = _seed(_notif())
    assert client.post(f"/notifications/{nid}/dismiss").status_code == 200
    body = client.get("/notifications").json()
    assert body["items"] == []
    assert body["unread"] == 0


def test_read_all_marks_everything(client: TestClient) -> None:
    _seed(_notif(dedup_key="a"))
    _seed(_notif(dedup_key="b"))
    r = client.post("/notifications/read-all")
    assert r.status_code == 200
    assert r.json()["updated"] == 2
    assert client.get("/notifications").json()["unread"] == 0


def test_expired_excluded_from_list(client: TestClient) -> None:
    _seed(_notif(dedup_key="old", expires_at=datetime(2020, 1, 1, tzinfo=UTC)))
    assert client.get("/notifications").json()["items"] == []
