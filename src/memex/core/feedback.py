"""Persistencia de `inbox_feedback` — feedback manual rápido por mensaje (SQL puro, sin red).

Captura categorías rápidas (`kinds`) + nota, con un snapshot de lo observado en `metadata`. Solo
captura: NO corrige ni dispara auto-mejora (diferido). Upsert por `inbox_id` (un feedback por
mensaje, se actualiza al re-reportar). Análogo a `core.media`/`core.deadletter`.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

#: Categorías rápidas válidas (calidad del procesamiento). Las etiquetas amigables van en el front.
FEEDBACK_KINDS: frozenset[str] = frozenset(
    {
        "missing_data",  # no registró todos los datos importantes
        "missed_important",  # no destacó / notificó algo importante
        "bad_summary",  # resumen incorrecto o incompleto
        "wrong_extraction",  # extracción incorrecta
        "bad_ocr",  # OCR / adjunto mal leído
        "other",  # otro (ver nota)
    }
)


#: Estados válidos de un feedback en la gestión/calibración (`open` al capturar; el usuario lo
#: mueve a `reviewed`/`dismissed` desde "Calidad y precisión").
FEEDBACK_STATUSES: frozenset[str] = frozenset({"open", "reviewed", "dismissed"})


class InvalidFeedbackError(ValueError):
    """`kinds` vacío/categorías inválidas, o estado fuera de `FEEDBACK_STATUSES`."""


def _validate_kinds(kinds: list[str]) -> list[str]:
    if not kinds:
        raise InvalidFeedbackError("kinds no puede estar vacío")
    invalid = [k for k in kinds if k not in FEEDBACK_KINDS]
    if invalid:
        raise InvalidFeedbackError(
            f"categorías inválidas: {invalid}; válidas: {sorted(FEEDBACK_KINDS)}"
        )
    # Dedup preservando orden.
    return list(dict.fromkeys(kinds))


def record_feedback(
    conn: Connection,
    *,
    user_id: int,
    inbox_id: int,
    kinds: list[str],
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert del feedback de un mensaje (uno por `inbox_id`). Devuelve la fila resultante."""
    clean = _validate_kinds(kinds)
    row = (
        conn.execute(
            text(
                """
                INSERT INTO inbox_feedback (user_id, inbox_id, kinds, note, metadata)
                VALUES (:uid, :iid, :kinds, :note, CAST(:meta AS JSONB))
                ON CONFLICT (inbox_id) DO UPDATE
                    SET kinds = EXCLUDED.kinds, note = EXCLUDED.note,
                        metadata = EXCLUDED.metadata, status = 'open', updated_at = NOW()
                RETURNING kinds, note, metadata, status, created_at, updated_at
                """
            ),
            {
                "uid": user_id,
                "iid": inbox_id,
                "kinds": clean,
                "note": note,
                "meta": json.dumps(metadata or {}),
            },
        )
        .mappings()
        .first()
    )
    assert row is not None  # RETURNING de un upsert siempre devuelve fila
    return dict(row)


def get_feedback(conn: Connection, inbox_id: int) -> dict[str, Any] | None:
    """Feedback de un mensaje (o None)."""
    row = (
        conn.execute(
            text(
                "SELECT kinds, note, metadata, status, created_at, updated_at "
                "FROM inbox_feedback WHERE inbox_id = :iid"
            ),
            {"iid": inbox_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def list_feedback(
    conn: Connection, *, user_id: int, status: str | None = "open", limit: int = 500
) -> list[dict[str, Any]]:
    """Feedback acumulado del user, con contexto del mensaje (remitente/asunto/tier)."""
    where = ["f.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("f.status = :st")
        params["st"] = status
    rows = (
        conn.execute(
            text(
                f"""
                SELECT f.inbox_id, f.kinds, f.note, f.metadata, f.status,
                       f.created_at, f.updated_at,
                       i.payload->>'subject' AS subject,
                       i.payload->'from'->>'email' AS from_email,
                       c.tier AS tier
                FROM inbox_feedback f
                JOIN inbox i ON i.id = f.inbox_id
                LEFT JOIN classifications c ON c.inbox_id = f.inbox_id
                WHERE {" AND ".join(where)}
                ORDER BY f.updated_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def set_feedback_status(
    conn: Connection, *, user_id: int, inbox_id: int, status: str
) -> dict[str, Any] | None:
    """Cambia el estado de un feedback (acotado al dueño). Devuelve la fila o None si no existe.

    Solo mueve el estado (gestión/calibración); no toca `kinds`/`note`/`metadata`. Re-reportar el
    mensaje lo resetea a `open` (ver `record_feedback`).
    """
    if status not in FEEDBACK_STATUSES:
        raise InvalidFeedbackError(
            f"estado inválido: {status!r}; válidos: {sorted(FEEDBACK_STATUSES)}"
        )
    row = (
        conn.execute(
            text(
                """
                UPDATE inbox_feedback
                   SET status = :st, updated_at = NOW()
                 WHERE inbox_id = :iid AND user_id = :uid
                RETURNING kinds, note, metadata, status, created_at, updated_at
                """
            ),
            {"st": status, "iid": inbox_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None
