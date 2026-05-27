from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.inbox import claim_batch, insert_record, mark_processed
from memex.core.source import SourceRecord


def _rec(
    external_id: str = "x1", payload: dict | None = None, dedupe_keys: list[str] | None = None
) -> SourceRecord:
    return SourceRecord(
        external_id=external_id,
        occurred_at=datetime(2026, 5, 23, 10, 0, tzinfo=UTC),
        payload=payload or {"hello": "world"},
        dedupe_keys=dedupe_keys or [],
    )


def test_insert_record_succeeds(conn: Any, seed_source: dict) -> None:
    r = insert_record(conn, user_id=1, source_id=seed_source["id"], record=_rec())
    assert r.inserted is True
    assert r.id is not None
    assert r.reason is None


def test_insert_record_duplicate_is_noop(conn: Any, seed_source: dict) -> None:
    rec = _rec("dup1")
    r1 = insert_record(conn, user_id=1, source_id=seed_source["id"], record=rec)
    r2 = insert_record(conn, user_id=1, source_id=seed_source["id"], record=rec)
    assert r1.inserted is True
    assert r2.inserted is False
    assert r2.reason == "duplicate"
    count = conn.execute(text("SELECT COUNT(*) FROM inbox")).scalar()
    assert count == 1


def test_insert_record_rejects_unknown_source(conn: Any) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        insert_record(conn, user_id=1, source_id=9999, record=_rec())


def test_insert_record_rejects_cross_tenant(conn: Any, seed_source: dict, seed_user2: int) -> None:
    with pytest.raises(ValueError, match="does not belong to user"):
        insert_record(conn, user_id=seed_user2, source_id=seed_source["id"], record=_rec())


def test_dedupe_keys_persisted(conn: Any, seed_source: dict) -> None:
    rec = _rec("k1", dedupe_keys=["msgid:<a@x>", "imap:1:5"])
    r = insert_record(conn, user_id=1, source_id=seed_source["id"], record=rec)
    rows = (
        conn.execute(
            text("SELECT key FROM inbox_dedupe_keys WHERE inbox_id = :id ORDER BY key"),
            {"id": r.id},
        )
        .scalars()
        .all()
    )
    assert rows == ["imap:1:5", "msgid:<a@x>"]


def test_dedupe_keys_collision_between_users_allowed(
    conn: Any, seed_source: dict, seed_user2: int
) -> None:
    """Two users may have the same dedupe key (PK is (user_id, key))."""
    # source for user 2
    src2 = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:uid, 'other-src', 'imap') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    shared_key = "msgid:<same@host>"
    insert_record(
        conn, user_id=1, source_id=seed_source["id"], record=_rec("u1", dedupe_keys=[shared_key])
    )
    insert_record(
        conn, user_id=seed_user2, source_id=src2, record=_rec("u2", dedupe_keys=[shared_key])
    )
    count = conn.execute(
        text("SELECT COUNT(*) FROM inbox_dedupe_keys WHERE key = :k"), {"k": shared_key}
    ).scalar()
    assert count == 2


def test_claim_batch_returns_pending_only(conn: Any, seed_source: dict) -> None:
    for i in range(3):
        insert_record(conn, user_id=1, source_id=seed_source["id"], record=_rec(f"p{i}"))
    batch = claim_batch(conn, user_id=1, limit=10)
    assert len(batch) == 3
    # mark one processed, claim again
    mark_processed(conn, user_id=1, inbox_id=batch[0]["id"], error=None)
    remaining = claim_batch(conn, user_id=1, limit=10)
    assert len(remaining) == 2


def test_claim_batch_filters_by_source(conn: Any, seed_source: dict) -> None:
    src2 = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (1, 'imap-other', 'imap') RETURNING id"
        )
    ).scalar()
    insert_record(conn, user_id=1, source_id=seed_source["id"], record=_rec("a"))
    insert_record(conn, user_id=1, source_id=src2, record=_rec("b"))
    only_first = claim_batch(conn, user_id=1, source_id=seed_source["id"], limit=10)
    assert {r["external_id"] for r in only_first} == {"a"}


def test_claim_batch_scopes_by_user(conn: Any, seed_source: dict, seed_user2: int) -> None:
    src2 = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:uid, 'u2-src', 'imap') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    insert_record(conn, user_id=1, source_id=seed_source["id"], record=_rec("for-u1"))
    insert_record(conn, user_id=seed_user2, source_id=src2, record=_rec("for-u2"))
    u1 = claim_batch(conn, user_id=1, limit=10)
    u2 = claim_batch(conn, user_id=seed_user2, limit=10)
    assert {r["external_id"] for r in u1} == {"for-u1"}
    assert {r["external_id"] for r in u2} == {"for-u2"}


def test_mark_processed_records_error_and_bumps_attempts(conn: Any, seed_source: dict) -> None:
    r = insert_record(conn, user_id=1, source_id=seed_source["id"], record=_rec("e1"))
    mark_processed(conn, user_id=1, inbox_id=r.id, error="parse failed")
    row = (
        conn.execute(
            text("SELECT processed_at, process_error, attempts FROM inbox WHERE id = :id"),
            {"id": r.id},
        )
        .mappings()
        .first()
    )
    assert row["processed_at"] is not None
    assert row["process_error"] == "parse failed"
    assert row["attempts"] == 1


def test_mark_processed_does_not_affect_other_users_rows(
    conn: Any, seed_source: dict, seed_user2: int
) -> None:
    src2 = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type) "
            "VALUES (:uid, 'u2-src', 'imap') RETURNING id"
        ),
        {"uid": seed_user2},
    ).scalar()
    r2 = insert_record(conn, user_id=seed_user2, source_id=src2, record=_rec("u2row"))
    # user 1 tries to mark user 2's row — no rows affected
    mark_processed(conn, user_id=1, inbox_id=r2.id, error=None)
    still_pending = conn.execute(
        text("SELECT processed_at FROM inbox WHERE id = :id"), {"id": r2.id}
    ).scalar()
    assert still_pending is None
