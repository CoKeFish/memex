"""Schema check para la tabla `classifications` (migración 0005).

No hay consumidor todavía (el classifier es futuro), así que verificamos
directamente que el DDL de la 0005 impone lo diseñado: una clasificación por
mensaje (UNIQUE inbox_id), `tier` acotado por CHECK, y FKs a users/inbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection


def _seed_inbox(source_id: int, external_id: str = "m1") -> int:
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


def _classify(inbox_id: int, tier: str) -> None:
    with connection() as c:
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": inbox_id, "tier": tier},
        )


def test_classification_insert_and_read_back(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"])
    _classify(inbox_id, "batch")
    with connection() as c:
        tier = c.execute(
            text("SELECT tier FROM classifications WHERE inbox_id = :iid"),
            {"iid": inbox_id},
        ).scalar()
    assert tier == "batch"


def test_classification_is_unique_per_inbox(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"])
    _classify(inbox_id, "batch")
    with pytest.raises(IntegrityError):
        _classify(inbox_id, "individual")  # mismo inbox_id → viola UNIQUE


def test_classification_rejects_unknown_tier(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"])
    with pytest.raises(IntegrityError):
        _classify(inbox_id, "basura")  # viola el CHECK
