"""Tests del router /review (cola de revisión: dead-letter real + reencolar). Cliente con auth off.

work_item_failures/inbox se limpian por cascada de FK `user_id` cuando `_reset_tables` trunca
`users` (conftest). El dead-letter solo expone status='review' (lo 'failing' se sigue reintentando).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_source(name: str = "s", user_id: int = 1) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (:u, :n, 'imap') RETURNING id"
                ),
                {"u": user_id, "n": name},
            ).scalar_one()
        )


def _seed_inbox(source_id: int, payload: dict[str, Any], user_id: int = 1) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (:u, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"u": user_id, "s": source_id, "e": uuid.uuid4().hex, "p": json.dumps(payload)},
            ).scalar_one()
        )


def _seed_failure(
    inbox_id: int,
    *,
    stage: str = "summarize",
    status: str = "review",
    user_id: int = 1,
    error: str = "boom",
) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO work_item_failures "
                "(user_id, stage, inbox_id, attempts, last_error, status) "
                "VALUES (:u, :st, :i, 3, :e, :status)"
            ),
            {"u": user_id, "st": stage, "i": inbox_id, "e": error, "status": status},
        )


def test_dead_letter_empty(client: Any) -> None:
    assert client.get("/review/dead-letter").json() == []


def test_dead_letter_lists_review_items_with_context(client: Any) -> None:
    sid = _seed_source()
    iid = _seed_inbox(sid, {"subject": "Factura", "body_text": "total $100"})
    _seed_failure(iid, stage="summarize", status="review", error="JSON inválido")
    # 'failing' (todavía no cruzó el umbral) NO aparece en la cola.
    iid2 = _seed_inbox(sid, {"text": "otro"})
    _seed_failure(iid2, stage="extract", status="failing")

    items = client.get("/review/dead-letter").json()
    assert len(items) == 1
    it = items[0]
    assert it["stage"] == "summarize"
    assert it["inbox_id"] == iid
    assert it["attempts"] == 3
    assert it["last_error"] == "JSON inválido"
    assert "Factura" in it["preview"]
    assert "total $100" in it["preview"]


def test_requeue_removes_from_review(client: Any) -> None:
    sid = _seed_source()
    iid = _seed_inbox(sid, {"text": "x"})
    _seed_failure(iid, stage="summarize", status="review")

    r = client.post(f"/review/dead-letter/summarize/{iid}/requeue")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert client.get("/review/dead-letter").json() == []
    # ya no está en revisión → 404
    assert client.post(f"/review/dead-letter/summarize/{iid}/requeue").status_code == 404


def test_requeue_invalid_stage_is_422(client: Any) -> None:
    assert client.post("/review/dead-letter/nope/1/requeue").status_code == 422


def test_dead_letter_cross_tenant(client: Any, seed_user2: int) -> None:
    sid = _seed_source("theirs", user_id=seed_user2)
    iid = _seed_inbox(sid, {"text": "theirs"}, user_id=seed_user2)
    _seed_failure(iid, status="review", user_id=seed_user2)
    assert client.get("/review/dead-letter").json() == []
