"""Tests de POST /inbox/{id}/process (clasificación determinista de un mensaje puntual)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _insert_inbox(source_id: int, eid: str, payload: dict[str, Any]) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, '2026-05-31T10:00:00Z', CAST(:p AS JSONB))
                RETURNING id
                """
            ),
            {"sid": source_id, "eid": eid, "p": json.dumps(payload)},
        ).scalar()
    assert isinstance(iid, int)
    return iid


def test_process_classifies_batch(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _insert_inbox(seed_source["id"], "p1", {"subject": "hola", "body_text": "x"})
    r = client.post(f"/inbox/{iid}/process")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "inbox_id": iid,
        "tier": "batch",
        "reason": "default",
        "classified": True,
        "already": False,
    }
    # El detalle ahora expone la clasificación.
    detail = client.get(f"/inbox/{iid}").json()
    assert detail["classification"]["tier"] == "batch"


def test_process_classifies_blacklist(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _insert_inbox(
        seed_source["id"], "p2", {"subject": "promo", "list_unsubscribe": "<mailto:x>"}
    )
    body = client.post(f"/inbox/{iid}/process").json()
    assert body["tier"] == "blacklist"
    assert body["classified"] is True


def test_process_is_idempotent(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _insert_inbox(seed_source["id"], "p3", {"subject": "hola", "body_text": "x"})
    first = client.post(f"/inbox/{iid}/process").json()
    assert first["classified"] is True and first["already"] is False
    second = client.post(f"/inbox/{iid}/process").json()
    assert second["classified"] is False
    assert second["already"] is True
    assert second["tier"] == "batch"


def test_process_unknown_is_404(client: Any) -> None:
    assert client.post("/inbox/999999/process").status_code == 404


def test_detail_without_classification_is_null(client: Any, seed_source: dict[str, Any]) -> None:
    iid = _insert_inbox(seed_source["id"], "p4", {"subject": "hola", "body_text": "x"})
    assert client.get(f"/inbox/{iid}").json()["classification"] is None
