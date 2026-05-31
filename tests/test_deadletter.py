"""Dead-letter de los workers LLM (gap c): contador de fallos, flip a 'review', list, requeue.

Contra la DB sembrada (sin red): ejercita memex.core.deadletter directo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.deadletter import (
    MAX_WORK_ATTEMPTS,
    STAGE_EXTRACT,
    STAGE_SUMMARIZE,
    list_review,
    record_failures,
    requeue,
)
from memex.db import connection


def _new_source(name: str = "dl-src", source_type: str = "imap") -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_inbox(source_id: int, ext: str) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST('{}' AS JSONB)) RETURNING id
                """
            ),
            {"sid": source_id, "eid": ext, "occ": datetime(2026, 5, 28, 12, 0, tzinfo=UTC)},
        ).scalar()
    assert iid is not None
    return int(iid)


def _row(stage: str, inbox_id: int) -> dict[str, Any] | None:
    with connection() as c:
        r = (
            c.execute(
                text(
                    "SELECT attempts, status FROM work_item_failures "
                    "WHERE stage = :s AND inbox_id = :i"
                ),
                {"s": stage, "i": inbox_id},
            )
            .mappings()
            .first()
        )
    return dict(r) if r is not None else None


def test_failures_increment_then_flip_to_review() -> None:
    iid = _seed_inbox(_new_source(), "m1")
    for n in range(1, MAX_WORK_ATTEMPTS):  # 1 .. MAX-1 → todavía 'failing'
        record_failures(1, STAGE_EXTRACT, [iid], f"boom {n}")
        assert _row(STAGE_EXTRACT, iid) == {"attempts": n, "status": "failing"}
    record_failures(1, STAGE_EXTRACT, [iid], "boom final")  # MAX-ésimo → 'review'
    assert _row(STAGE_EXTRACT, iid) == {"attempts": MAX_WORK_ATTEMPTS, "status": "review"}


def test_list_review_and_requeue() -> None:
    iid = _seed_inbox(_new_source(), "m1")
    for _ in range(MAX_WORK_ATTEMPTS):
        record_failures(1, STAGE_EXTRACT, [iid], "veneno")

    items = list_review(1, STAGE_EXTRACT)
    assert [it["inbox_id"] for it in items] == [iid]
    assert items[0]["attempts"] == MAX_WORK_ATTEMPTS

    assert requeue(1, STAGE_EXTRACT, iid) is True  # borra la fila → vuelve al work-set
    assert _row(STAGE_EXTRACT, iid) is None
    assert list_review(1, STAGE_EXTRACT) == []
    assert requeue(1, STAGE_EXTRACT, iid) is False  # ya no estaba


def test_stages_are_independent() -> None:
    iid = _seed_inbox(_new_source(), "m1")
    for _ in range(MAX_WORK_ATTEMPTS):
        record_failures(1, STAGE_EXTRACT, [iid], "x")
    # mismo inbox, OTRA etapa → contador propio (no lo arrastra el de extract).
    assert _row(STAGE_SUMMARIZE, iid) is None
    record_failures(1, STAGE_SUMMARIZE, [iid], "y")
    assert _row(STAGE_SUMMARIZE, iid) == {"attempts": 1, "status": "failing"}
    extract_row = _row(STAGE_EXTRACT, iid)
    assert extract_row is not None and extract_row["status"] == "review"


def test_window_bumps_every_message() -> None:
    """Un fallo de ventana suma a CADA mensaje (por inbox_id; daño colateral del veneno)."""
    sid = _new_source()
    i1 = _seed_inbox(sid, "m1")
    i2 = _seed_inbox(sid, "m2")
    record_failures(1, STAGE_EXTRACT, [i1, i2], "ventana falló")
    assert _row(STAGE_EXTRACT, i1) == {"attempts": 1, "status": "failing"}
    assert _row(STAGE_EXTRACT, i2) == {"attempts": 1, "status": "failing"}
