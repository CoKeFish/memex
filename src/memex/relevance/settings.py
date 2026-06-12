"""Settings del gate de relevancia (`relevance_gate_settings`) — una fila por usuario.

Tabla PROPIA y no `module_settings`: el gate no es un InterestModule (un slug ahí rompería
`resolve()` del registry y `PATCH /modules/{slug}`). Patrón `scheduler_settings`: la DB manda
en runtime, sin fila → defaults APAGADOS (procesamiento apagado por default).

`mode` es la perilla del experimento del dueño: `per_window` (1 llamada LLM por ventana con
veredictos por mensaje) vs `per_message` (1 llamada por correo).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

GATE_MODES = ("per_window", "per_message")
_DEFAULT_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class GateSettings:
    """Settings resueltos del gate para un usuario."""

    enabled: bool = False
    mode: str = "per_window"
    model: str = _DEFAULT_MODEL


def get_settings(conn: Connection, user_id: int) -> GateSettings:
    """Settings del gate del usuario; sin fila → defaults apagados."""
    row = (
        conn.execute(
            text("SELECT enabled, mode, model FROM relevance_gate_settings WHERE user_id = :uid"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return GateSettings()
    return GateSettings(
        enabled=bool(row["enabled"]), mode=str(row["mode"]), model=str(row["model"])
    )


def upsert_settings(
    conn: Connection,
    user_id: int,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    model: str | None = None,
) -> GateSettings:
    """Upsert PARCIAL (solo los campos pasados); devuelve los settings resultantes.

    `mode` inválido → ValueError (el CHECK de la DB también lo rechazaría, pero el error
    de capa de aplicación es accionable para API/CLI).
    """
    if mode is not None and mode not in GATE_MODES:
        raise ValueError(f"mode inválido: {mode!r}; válidos: {GATE_MODES}")
    current = get_settings(conn, user_id)
    resolved = GateSettings(
        enabled=current.enabled if enabled is None else enabled,
        mode=current.mode if mode is None else mode,
        model=current.model if model is None else model,
    )
    conn.execute(
        text(
            """
            INSERT INTO relevance_gate_settings (user_id, enabled, mode, model)
            VALUES (:uid, :enabled, :mode, :model)
            ON CONFLICT (user_id) DO UPDATE
                SET enabled = EXCLUDED.enabled, mode = EXCLUDED.mode, model = EXCLUDED.model,
                    updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "enabled": resolved.enabled,
            "mode": resolved.mode,
            "model": resolved.model,
        },
    )
    return resolved
