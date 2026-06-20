"""Settings del orquestador de extracción (`extraction_settings`), una fila por usuario.

Patrón `relevance/settings.py`: la DB manda en runtime; sin fila → defaults (ruteo ENCENDIDO,
el comportamiento previo a esta tabla). `routing_enabled` gatea el ruteo LLM por ventana en
`orchestrator._route`: en FALSE se saltan las llamadas de ruteo y se extraen TODOS los módulos
candidatos juntos (los que `candidates_for_kind` deja pasar por tipo de mensaje). El pre-filtro
determinista por tipo NO depende de esta perilla.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class ExtractionSettings:
    """Settings del orquestador de extracción para un usuario.

    `routing_enabled`: si True (default), un paso LLM elige por ventana qué módulos candidatos
    extraer; si False, se extraen todos los candidatos sin llamada de ruteo.
    """

    routing_enabled: bool = True


def get_extraction_settings(conn: Connection, user_id: int) -> ExtractionSettings:
    """Settings de extracción del usuario; sin fila → defaults (ruteo encendido)."""
    row = (
        conn.execute(
            text("SELECT routing_enabled FROM extraction_settings WHERE user_id = :uid"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return ExtractionSettings()
    return ExtractionSettings(routing_enabled=bool(row["routing_enabled"]))


def upsert_extraction_settings(
    conn: Connection, user_id: int, *, routing_enabled: bool | None = None
) -> ExtractionSettings:
    """Upsert PARCIAL (solo los campos pasados); devuelve los settings resultantes."""
    current = get_extraction_settings(conn, user_id)
    resolved = ExtractionSettings(
        routing_enabled=(current.routing_enabled if routing_enabled is None else routing_enabled),
    )
    conn.execute(
        text(
            """
            INSERT INTO extraction_settings (user_id, routing_enabled)
            VALUES (:uid, :routing_enabled)
            ON CONFLICT (user_id) DO UPDATE
                SET routing_enabled = EXCLUDED.routing_enabled, updated_at = NOW()
            """
        ),
        {"uid": user_id, "routing_enabled": resolved.routing_enabled},
    )
    return resolved
