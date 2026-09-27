"""Schema check para `summaries` + `summary_inbox_links` (migración 0006).

El consumidor es relations/summary.py (run_summaries/persist_summary escriben `summaries`); acá
verificamos que el DDL impone lo diseñado: `tier` acotado por CHECK, FK a users, y el puente N:M
(summary_inbox_links) con PK compuesta + cascadas hacia summary e inbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection


def _seed_inbox(source_id: int, external_id: str) -> int:
    with connection() as c:
        inbox_id = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occurred, CAST(:payload AS JSONB))
                RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": external_id,
                "occurred": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                "payload": "{}",
            },
        ).scalar()
    assert inbox_id is not None
    return int(inbox_id)


def _new_summary(tier: str, content: str = "resumen") -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content) "
                "VALUES (1, :tier, :content) RETURNING id"
            ),
            {"tier": tier, "content": content},
        ).scalar()
    assert sid is not None
    return int(sid)


def _link(summary_id: int, inbox_id: int) -> None:
    with connection() as c:
        c.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:sid, :iid)"),
            {"sid": summary_id, "iid": inbox_id},
        )


def test_summary_insert_and_read_back() -> None:
    summary_id = _new_summary("individual", "hola")
    with connection() as c:
        row = (
            c.execute(
                text("SELECT tier, content FROM summaries WHERE id = :id"),
                {"id": summary_id},
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["tier"] == "individual"
    assert row["content"] == "hola"


def test_summary_rejects_unknown_tier() -> None:
    with pytest.raises(IntegrityError):
        _new_summary("blacklist")  # blacklist NO produce summary → viola el CHECK


def test_batch_summary_links_many_messages(seed_source: dict[str, Any]) -> None:
    """Un summary `batch` cubre N mensajes (el caso que justifica el N:M)."""
    summary_id = _new_summary("batch")
    ids = [_seed_inbox(seed_source["id"], f"m{i}") for i in range(3)]
    for iid in ids:
        _link(summary_id, iid)
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM summary_inbox_links WHERE summary_id = :sid"),
            {"sid": summary_id},
        ).scalar()
    assert n == 3


def test_link_is_unique_per_pair(seed_source: dict[str, Any]) -> None:
    summary_id = _new_summary("batch")
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _link(summary_id, inbox_id)
    with pytest.raises(IntegrityError):
        _link(summary_id, inbox_id)  # mismo (summary_id, inbox_id) → viola la PK


def test_links_cascade_when_summary_deleted(seed_source: dict[str, Any]) -> None:
    summary_id = _new_summary("batch")
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _link(summary_id, inbox_id)
    with connection() as c:
        c.execute(text("DELETE FROM summaries WHERE id = :id"), {"id": summary_id})
        remaining = c.execute(
            text("SELECT count(*) FROM summary_inbox_links WHERE summary_id = :sid"),
            {"sid": summary_id},
        ).scalar()
    assert remaining == 0


def test_links_cascade_when_inbox_deleted(seed_source: dict[str, Any]) -> None:
    summary_id = _new_summary("batch")
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _link(summary_id, inbox_id)
    with connection() as c:
        c.execute(text("DELETE FROM inbox WHERE id = :id"), {"id": inbox_id})
        remaining = c.execute(
            text("SELECT count(*) FROM summary_inbox_links WHERE inbox_id = :iid"),
            {"iid": inbox_id},
        ).scalar()
    assert remaining == 0
