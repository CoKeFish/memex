"""Overrides de tier por remitente — "no procesar" un remitente (acción asistida de calidad).

El usuario fuerza el `tier` de los mensajes FUTUROS de un remitente (por email). Caso típico:
`blacklist` = "no procesar" (se guarda en inbox pero sin gasto LLM de resumen/extracción;
conserva la memoria, ahorra cómputo). Lo consume `classifier.worker.run_classification` ANTES de
`classify()`. SQL puro, sin red. Análogo a `core.feedback` / `core.filters`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text


def sender_email(payload: dict[str, Any]) -> str | None:
    """Email del remitente (normalizado a minúsculas) o None — solo email (chat/social no tienen).

    Espeja la clave de agrupación de `quality.relevance` para email: `lower(payload.from.email)`.
    """
    frm = payload.get("from")
    if isinstance(frm, dict):
        email = frm.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip().lower()
    return None


def load_overrides(conn: Connection, user_id: int) -> dict[str, str]:
    """Mapa `sender_email → tier` de los overrides del user (para resolver en O(1) por mensaje)."""
    rows = conn.execute(
        text("SELECT sender_email, tier FROM sender_tier_overrides WHERE user_id = :uid"),
        {"uid": user_id},
    ).all()
    return {str(email): str(tier) for email, tier in rows}


def set_override(
    conn: Connection,
    *,
    user_id: int,
    sender_email: str,
    tier: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Upsert del override de un remitente (uno por `sender_email`). Devuelve la fila resultante."""
    row = (
        conn.execute(
            text(
                """
                INSERT INTO sender_tier_overrides (user_id, sender_email, tier, reason)
                VALUES (:uid, :email, :tier, :reason)
                ON CONFLICT (user_id, sender_email) DO UPDATE
                    SET tier = EXCLUDED.tier, reason = EXCLUDED.reason, updated_at = NOW()
                RETURNING sender_email, tier, reason, created_at, updated_at
                """
            ),
            {"uid": user_id, "email": sender_email.strip().lower(), "tier": tier, "reason": reason},
        )
        .mappings()
        .first()
    )
    assert row is not None  # RETURNING de un upsert siempre devuelve fila
    return dict(row)


def clear_override(conn: Connection, *, user_id: int, sender_email: str) -> bool:
    """Borra el override de un remitente (vuelve a la heurística). False si no existía."""
    n = conn.execute(
        text("DELETE FROM sender_tier_overrides WHERE user_id = :uid AND sender_email = :email"),
        {"uid": user_id, "email": sender_email.strip().lower()},
    ).rowcount
    return n > 0
