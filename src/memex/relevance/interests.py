"""CRUD de intereses personales (`personal_interests`) — el contexto que consume el gate.

Texto libre por interés (ej. «descuentos de Steam»). `enabled` permite apagar un interés sin
perderlo. SQL puro con `conn` inyectada (patrón `core/relevance_marks.py`); el conflicto del
UNIQUE (user_id, text) lo maneja el caller (API → 409).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

_ROW_COLS = "id, text, enabled, created_at, updated_at"


def list_interests(
    conn: Connection, user_id: int, *, enabled_only: bool = False
) -> list[dict[str, Any]]:
    """Intereses del usuario, más viejos primero (orden estable para el prompt del gate)."""
    where = "user_id = :uid" + (" AND enabled" if enabled_only else "")
    rows = (
        conn.execute(
            text(f"SELECT {_ROW_COLS} FROM personal_interests WHERE {where} ORDER BY id"),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def create_interest(conn: Connection, user_id: int, text_value: str) -> dict[str, Any]:
    """Alta de un interés. Texto vacío → ValueError; duplicado → IntegrityError (caller)."""
    cleaned = text_value.strip()
    if not cleaned:
        raise ValueError("el interés no puede ser vacío")
    row = (
        conn.execute(
            text(
                "INSERT INTO personal_interests (user_id, text) VALUES (:uid, :text) "
                f"RETURNING {_ROW_COLS}"
            ),
            {"uid": user_id, "text": cleaned},
        )
        .mappings()
        .first()
    )
    assert row is not None  # RETURNING de un INSERT siempre devuelve fila
    return dict(row)


def update_interest(
    conn: Connection,
    interest_id: int,
    user_id: int,
    *,
    text_value: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    """Update parcial (texto y/o enabled). None si no existe (o es de otro usuario)."""
    sets: list[str] = ["updated_at = NOW()"]
    params: dict[str, Any] = {"id": interest_id, "uid": user_id}
    if text_value is not None:
        cleaned = text_value.strip()
        if not cleaned:
            raise ValueError("el interés no puede ser vacío")
        sets.append("text = :text")
        params["text"] = cleaned
    if enabled is not None:
        sets.append("enabled = :enabled")
        params["enabled"] = enabled
    row = (
        conn.execute(
            text(
                f"UPDATE personal_interests SET {', '.join(sets)} "
                f"WHERE id = :id AND user_id = :uid RETURNING {_ROW_COLS}"
            ),
            params,
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def delete_interest(conn: Connection, interest_id: int, user_id: int) -> bool:
    """Borra un interés (acotado al dueño). False si no existía."""
    n = conn.execute(
        text("DELETE FROM personal_interests WHERE id = :id AND user_id = :uid"),
        {"id": interest_id, "uid": user_id},
    ).rowcount
    return n > 0
