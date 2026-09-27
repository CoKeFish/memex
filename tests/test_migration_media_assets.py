"""Schema check para `media_assets` (migración 0009).

Verifica que el DDL impone lo diseñado: `ocr_status` acotado por CHECK, FK a users/inbox con
CASCADE, y `UNIQUE(inbox_id, sha256)` (atribución de una imagen a un mensaje).
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


def _insert_media(
    inbox_id: int,
    *,
    sha256: str = "abc",
    ocr_status: str = "pending",
    content_type: str = "image/png",
) -> int:
    with connection() as c:
        mid = c.execute(
            text(
                """
                INSERT INTO media_assets
                  (user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes,
                   filename, ocr_status)
                VALUES (1, :iid, :sha, :key, 'memex-media', :ctype, 10, 'f.png', :status)
                RETURNING id
                """
            ),
            {
                "iid": inbox_id,
                "sha": sha256,
                "key": f"media/1/{sha256}.png",
                "ctype": content_type,
                "status": ocr_status,
            },
        ).scalar()
    assert mid is not None
    return int(mid)


def test_media_insert_and_read_back(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _insert_media(inbox_id, sha256="deadbeef")
    with connection() as c:
        row = (
            c.execute(
                text(
                    "SELECT ocr_status, content_type, bucket FROM media_assets WHERE inbox_id = :i"
                ),
                {"i": inbox_id},
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["ocr_status"] == "pending"
    assert row["content_type"] == "image/png"
    assert row["bucket"] == "memex-media"


def test_rejects_unknown_ocr_status(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    with pytest.raises(IntegrityError):
        _insert_media(inbox_id, ocr_status="bogus")  # viola el CHECK


def test_unique_inbox_sha(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _insert_media(inbox_id, sha256="same")
    with pytest.raises(IntegrityError):
        _insert_media(inbox_id, sha256="same")  # mismo (inbox_id, sha256) → viola UNIQUE


def test_same_sha_different_inbox_ok(seed_source: dict[str, Any]) -> None:
    """La misma imagen en dos mensajes = dos filas (atribución por-mensaje)."""
    i1 = _seed_inbox(seed_source["id"], "m1")
    i2 = _seed_inbox(seed_source["id"], "m2")
    _insert_media(i1, sha256="shared")
    _insert_media(i2, sha256="shared")
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM media_assets WHERE sha256 = 'shared'")).scalar()
    assert n == 2


def test_cascade_when_inbox_deleted(seed_source: dict[str, Any]) -> None:
    inbox_id = _seed_inbox(seed_source["id"], "m1")
    _insert_media(inbox_id)
    with connection() as c:
        c.execute(text("DELETE FROM inbox WHERE id = :id"), {"id": inbox_id})
        remaining = c.execute(
            text("SELECT count(*) FROM media_assets WHERE inbox_id = :i"), {"i": inbox_id}
        ).scalar()
    assert remaining == 0
