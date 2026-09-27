"""Persistencia de `relevance_marks` — override manual de relevancia por mensaje (SQL puro).

El usuario marca un mensaje como relevante / no relevante; su juicio gana sobre la heurística
para ESE mensaje (lo consume `quality.relevance` vía `COALESCE(is_relevant, produced_fact)`). Upsert
por `inbox_id` (una marca por mensaje, se actualiza al re-marcar). Análogo a `core.feedback`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text


def set_mark(
    conn: Connection,
    *,
    user_id: int,
    inbox_id: int,
    is_relevant: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    """Upsert de la marca de un mensaje (una por `inbox_id`). Devuelve la fila resultante."""
    row = (
        conn.execute(
            text(
                """
                INSERT INTO relevance_marks (user_id, inbox_id, is_relevant, reason)
                VALUES (:uid, :iid, :rel, :reason)
                ON CONFLICT (inbox_id) DO UPDATE
                    SET is_relevant = EXCLUDED.is_relevant, reason = EXCLUDED.reason,
                        updated_at = NOW()
                RETURNING is_relevant, reason, created_at, updated_at
                """
            ),
            {"uid": user_id, "iid": inbox_id, "rel": is_relevant, "reason": reason},
        )
        .mappings()
        .first()
    )
    assert row is not None  # RETURNING de un upsert siempre devuelve fila
    return dict(row)


def get_mark(conn: Connection, inbox_id: int) -> dict[str, Any] | None:
    """Marca de un mensaje (o None)."""
    row = (
        conn.execute(
            text(
                "SELECT is_relevant, reason, created_at, updated_at "
                "FROM relevance_marks WHERE inbox_id = :iid"
            ),
            {"iid": inbox_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def clear_mark(conn: Connection, *, user_id: int, inbox_id: int) -> bool:
    """Borra la marca de un mensaje (acotado al dueño). False si no existía."""
    n = conn.execute(
        text("DELETE FROM relevance_marks WHERE inbox_id = :iid AND user_id = :uid"),
        {"iid": inbox_id, "uid": user_id},
    ).rowcount
    return n > 0
