"""Settings del resolvedor contextual de identidades (`identidades_resolver_settings`).

Una fila por usuario (patrón `relevance/settings.py`): la DB manda en runtime, sin fila →
defaults APAGADOS. `resolver_enabled` gatea la fase contextual por-correo dentro de
`module.dedup`; `batch_maintenance_enabled` gatea el mantenimiento por lotes (organize + merge
phase-2) del ciclo del scheduler. Ambos arrancan en FALSE (se prenden tras validar). El proveedor
y el modelo los decide el registry por consumer (`identidades_resolve`), no esta tabla.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class ResolverSettings:
    """Settings resueltos del resolvedor para un usuario.

    `resolver_enabled`: corre la fase contextual por-correo (merge/jerarquía/contacto con LLM).
    `batch_maintenance_enabled`: corre organize + merge phase-2 por lotes en el scheduler.
    `min_confidence_merge`/`min_confidence_parent`: umbrales para aplicar una fusión o una
    pertenencia que propone el LLM. `max_calls_per_window`: tope de llamadas LLM por ventana.
    """

    resolver_enabled: bool = False
    batch_maintenance_enabled: bool = False
    min_confidence_merge: float = 0.75
    min_confidence_parent: float = 0.80
    max_calls_per_window: int = 16
    #: Derivación del padre efectivo desde el libro (`links.py`): mensajes distintos que debe
    #: sustentar un padre para calificar, o la confianza mínima si lo sustenta un solo mensaje.
    parent_min_messages: int = 2
    parent_single_min_confidence: float = 0.90


def get_settings(conn: Connection, user_id: int) -> ResolverSettings:
    """Settings del resolvedor del usuario; sin fila → defaults apagados."""
    row = (
        conn.execute(
            text(
                "SELECT resolver_enabled, batch_maintenance_enabled, min_confidence_merge, "
                "min_confidence_parent, max_calls_per_window, parent_min_messages, "
                "parent_single_min_confidence "
                "FROM identidades_resolver_settings WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return ResolverSettings()
    return ResolverSettings(
        resolver_enabled=bool(row["resolver_enabled"]),
        batch_maintenance_enabled=bool(row["batch_maintenance_enabled"]),
        min_confidence_merge=float(row["min_confidence_merge"]),
        min_confidence_parent=float(row["min_confidence_parent"]),
        max_calls_per_window=int(row["max_calls_per_window"]),
        parent_min_messages=int(row["parent_min_messages"]),
        parent_single_min_confidence=float(row["parent_single_min_confidence"]),
    )


def upsert_settings(
    conn: Connection,
    user_id: int,
    *,
    resolver_enabled: bool | None = None,
    batch_maintenance_enabled: bool | None = None,
    min_confidence_merge: float | None = None,
    min_confidence_parent: float | None = None,
    max_calls_per_window: int | None = None,
    parent_min_messages: int | None = None,
    parent_single_min_confidence: float | None = None,
) -> ResolverSettings:
    """Upsert PARCIAL (solo los campos pasados); devuelve los settings resultantes.

    Umbrales fuera de [0, 1] o `max_calls_per_window < 1` → ValueError (el CHECK de la DB también
    los rechaza, pero el error de capa de aplicación es accionable para API/CLI).
    """
    bounded = (
        ("min_confidence_merge", min_confidence_merge),
        ("min_confidence_parent", min_confidence_parent),
        ("parent_single_min_confidence", parent_single_min_confidence),
    )
    for name, val in bounded:
        if val is not None and not (0.0 <= val <= 1.0):
            raise ValueError(f"{name} inválido: {val} (rango 0..1)")
    if max_calls_per_window is not None and max_calls_per_window < 1:
        raise ValueError(f"max_calls_per_window inválido: {max_calls_per_window} (mínimo 1)")
    if parent_min_messages is not None and parent_min_messages < 1:
        raise ValueError(f"parent_min_messages inválido: {parent_min_messages} (mínimo 1)")
    current = get_settings(conn, user_id)
    resolved = ResolverSettings(
        resolver_enabled=(
            current.resolver_enabled if resolver_enabled is None else resolver_enabled
        ),
        batch_maintenance_enabled=(
            current.batch_maintenance_enabled
            if batch_maintenance_enabled is None
            else batch_maintenance_enabled
        ),
        min_confidence_merge=(
            current.min_confidence_merge if min_confidence_merge is None else min_confidence_merge
        ),
        min_confidence_parent=(
            current.min_confidence_parent
            if min_confidence_parent is None
            else min_confidence_parent
        ),
        max_calls_per_window=(
            current.max_calls_per_window if max_calls_per_window is None else max_calls_per_window
        ),
        parent_min_messages=(
            current.parent_min_messages if parent_min_messages is None else parent_min_messages
        ),
        parent_single_min_confidence=(
            current.parent_single_min_confidence
            if parent_single_min_confidence is None
            else parent_single_min_confidence
        ),
    )
    conn.execute(
        text(
            """
            INSERT INTO identidades_resolver_settings
                (user_id, resolver_enabled, batch_maintenance_enabled, min_confidence_merge,
                 min_confidence_parent, max_calls_per_window, parent_min_messages,
                 parent_single_min_confidence)
            VALUES (:uid, :resolver_enabled, :batch, :merge, :parent, :max_calls, :pmm, :psmc)
            ON CONFLICT (user_id) DO UPDATE
                SET resolver_enabled = EXCLUDED.resolver_enabled,
                    batch_maintenance_enabled = EXCLUDED.batch_maintenance_enabled,
                    min_confidence_merge = EXCLUDED.min_confidence_merge,
                    min_confidence_parent = EXCLUDED.min_confidence_parent,
                    max_calls_per_window = EXCLUDED.max_calls_per_window,
                    parent_min_messages = EXCLUDED.parent_min_messages,
                    parent_single_min_confidence = EXCLUDED.parent_single_min_confidence,
                    updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "resolver_enabled": resolved.resolver_enabled,
            "batch": resolved.batch_maintenance_enabled,
            "merge": resolved.min_confidence_merge,
            "parent": resolved.min_confidence_parent,
            "max_calls": resolved.max_calls_per_window,
            "pmm": resolved.parent_min_messages,
            "psmc": resolved.parent_single_min_confidence,
        },
    )
    return resolved
