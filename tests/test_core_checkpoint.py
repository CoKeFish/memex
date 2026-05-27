from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.core import checkpoint


def test_get_cursor_returns_none_when_unset(conn: Any, seed_source: dict[str, Any]) -> None:
    assert checkpoint.get_cursor(conn, seed_source["id"]) is None


def test_save_and_get_roundtrip(conn: Any, seed_source: dict[str, Any]) -> None:
    cur = {"uidvalidity": 7, "last_uid": 100}
    checkpoint.save_cursor(conn, seed_source["id"], cur)
    got = checkpoint.get_cursor(conn, seed_source["id"])
    assert got == cur


def test_save_overwrites_existing(conn: Any, seed_source: dict[str, Any]) -> None:
    checkpoint.save_cursor(conn, seed_source["id"], {"v": 1})
    checkpoint.save_cursor(conn, seed_source["id"], {"v": 2, "more": "data"})
    assert checkpoint.get_cursor(conn, seed_source["id"]) == {"v": 2, "more": "data"}


def test_checkpoints_isolated_by_source(conn: Any, seed_source: dict[str, Any]) -> None:
    src2 = conn.execute(
        text("INSERT INTO sources (user_id, name, type) VALUES (1, 'imap-2', 'imap') RETURNING id")
    ).scalar()
    checkpoint.save_cursor(conn, seed_source["id"], {"a": 1})
    checkpoint.save_cursor(conn, src2, {"b": 2})
    assert checkpoint.get_cursor(conn, seed_source["id"]) == {"a": 1}
    assert checkpoint.get_cursor(conn, src2) == {"b": 2}
