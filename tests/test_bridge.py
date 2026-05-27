"""Tests del bridge — endpoints /bridge/plugins/{name}/state, /cursor, /ingest."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def _record(eid: str) -> dict[str, Any]:
    return {
        "external_id": eid,
        "occurred_at": "2026-05-27T10:00:00Z",
        "payload": {"hello": "world", "n": eid},
        "dedupe_keys": [f"msgid:<{eid}@host>"],
    }


# ---------------- state ----------------


def test_state_creates_source_when_missing(client: Any) -> None:
    r = client.post("/bridge/plugins/outlook-test/state", json={"source_type": "outlook"})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["cursor"] is None
    assert isinstance(body["source_id"], int)


def test_state_returns_existing_source_unchanged(client: Any) -> None:
    first = client.post("/bridge/plugins/p1/state", json={"source_type": "imap"}).json()
    second = client.post("/bridge/plugins/p1/state", json={"source_type": "imap"}).json()
    assert second["source_id"] == first["source_id"]
    assert second["created"] is False


def test_state_rejects_malformed_plugin_name(client: Any) -> None:
    r = client.post("/bridge/plugins/has spaces/state", json={"source_type": "imap"})
    assert r.status_code in (400, 404)  # FastAPI puede rechazar el path antes
    r = client.post("/bridge/plugins/-leading-dash/state", json={"source_type": "imap"})
    assert r.status_code == 400


def test_state_returns_existing_cursor(client: Any) -> None:
    state = client.post("/bridge/plugins/p2/state", json={"source_type": "imap"}).json()
    cursor = {"last_received_at": "2026-05-27T10:00:00+00:00"}
    client.put("/bridge/plugins/p2/cursor", json={"cursor": cursor})
    re_state = client.post("/bridge/plugins/p2/state", json={"source_type": "imap"}).json()
    assert re_state["cursor"] == cursor
    assert re_state["source_id"] == state["source_id"]


# ---------------- cursor ----------------


def test_cursor_put_unknown_plugin_is_404(client: Any) -> None:
    r = client.put(
        "/bridge/plugins/never-registered/cursor",
        json={"cursor": {"x": 1}},
    )
    assert r.status_code == 404


def test_cursor_roundtrip(client: Any) -> None:
    client.post("/bridge/plugins/p3/state", json={"source_type": "imap"})
    cur = {"last_received_at": "2026-01-01T00:00:00+00:00"}
    r = client.put("/bridge/plugins/p3/cursor", json={"cursor": cur})
    assert r.status_code == 200
    assert r.json()["cursor"] == cur


# ---------------- ingest ----------------


def test_ingest_persists_and_counts(client: Any) -> None:
    state = client.post("/bridge/plugins/p4/state", json={"source_type": "outlook"}).json()
    sid = state["source_id"]
    r = client.post(
        "/bridge/plugins/p4/ingest",
        json={"records": [_record("a"), _record("b"), _record("a")]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"source_id": sid, "inserted": 2, "duplicates": 1, "errors": 0}

    with connection() as c:
        count = c.execute(
            text("SELECT COUNT(*) FROM inbox WHERE source_id = :s"), {"s": sid}
        ).scalar()
    assert count == 2


def test_ingest_unknown_plugin_is_404(client: Any) -> None:
    r = client.post(
        "/bridge/plugins/never-registered/ingest",
        json={"records": [_record("x")]},
    )
    assert r.status_code == 404


def test_ingest_empty_batch_ok(client: Any) -> None:
    client.post("/bridge/plugins/p5/state", json={"source_type": "imap"})
    r = client.post("/bridge/plugins/p5/ingest", json={"records": []})
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 0
    assert body["duplicates"] == 0
    assert body["errors"] == 0


def test_ingest_strips_source_id_field(client: Any) -> None:
    """Si llega source_id en el record, debe ser ignorado — el bridge usa el del URL."""
    client.post("/bridge/plugins/p6/state", json={"source_type": "imap"})
    # Forzamos un source_id ajeno en cada record para verificar que NO termina en otra source.
    poisoned = {**_record("p"), "source_id": 99999}
    r = client.post("/bridge/plugins/p6/ingest", json={"records": [poisoned]})
    assert r.status_code == 200
    sid = r.json()["source_id"]
    with connection() as c:
        row = c.execute(text("SELECT source_id FROM inbox WHERE external_id = 'p'")).first()
    assert row is not None
    assert row[0] == sid


def test_ingest_requires_auth_when_enforced(auth_client: Any) -> None:
    # Sin token: 401 ya en /state.
    r = auth_client.post("/bridge/plugins/auth-probe/state", json={"source_type": "imap"})
    assert r.status_code == 401
    # Con token correcto: 200.
    r2 = auth_client.post(
        "/bridge/plugins/auth-probe/state",
        json={"source_type": "imap"},
        headers={"Authorization": "Bearer secret-test"},
    )
    assert r2.status_code == 200


def test_state_isolates_per_user(client: Any, seed_user2: int, conn: Any) -> None:
    """Un plugin con el mismo nombre en otro user no debe ser visible."""
    other_id = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:u, 'shared', 'imap') RETURNING id"
        ),
        {"u": seed_user2},
    ).scalar()
    assert isinstance(other_id, int)
    r = client.post("/bridge/plugins/shared/state", json={"source_type": "imap"})
    assert r.status_code == 200
    mine = r.json()
    assert mine["source_id"] != other_id
    assert mine["created"] is True
