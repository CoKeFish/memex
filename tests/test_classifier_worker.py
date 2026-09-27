"""Worker de clasificación contra la DB (sembrada)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.classifier.worker import run_classification
from memex.db import connection


def _seed_inbox(source_id: int, external_id: str, payload: dict[str, Any]) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:payload AS JSONB))
                RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": external_id,
                "occ": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                "payload": json.dumps(payload),
            },
        ).scalar()
    assert iid is not None
    return int(iid)


def _tier_of(inbox_id: int) -> str | None:
    with connection() as c:
        return c.execute(
            text("SELECT tier FROM classifications WHERE inbox_id = :iid"),
            {"iid": inbox_id},
        ).scalar()


def test_classifies_bulk_and_default(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    news = _seed_inbox(sid, "n1", {"subject": "promo", "list_id": "<l.x.com>"})
    personal = _seed_inbox(sid, "p1", {"subject": "hola", "body_text": "qué tal"})

    stats = run_classification(1)

    assert stats.scanned == 2
    assert stats.classified == 2
    assert _tier_of(news) == "blacklist"
    assert _tier_of(personal) == "batch"


def test_idempotent(seed_source: dict[str, Any]) -> None:
    _seed_inbox(seed_source["id"], "n1", {"list_id": "<l.x.com>"})
    first = run_classification(1)
    second = run_classification(1)
    assert first.classified == 1
    assert second.scanned == 0  # ya clasificado → no se re-escanea
    assert second.classified == 0


def test_dry_run_writes_nothing(seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"], "n1", {"list_id": "<l.x.com>"})
    stats = run_classification(1, dry_run=True)
    assert stats.scanned == 1
    assert stats.by_tier.get("blacklist") == 1
    assert _tier_of(iid) is None  # no escribió nada


def test_source_filter(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    iid = _seed_inbox(sid, "p1", {"subject": "hola"})
    stats = run_classification(1, source_id=sid + 999)  # otro source → nada
    assert stats.scanned == 0
    assert _tier_of(iid) is None
