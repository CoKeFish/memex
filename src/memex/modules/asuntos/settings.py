"""Settings de asuntos (`asuntos_settings`): una fila por usuario, sin fila → defaults ENCENDIDOS.

Patrón `identidades_resolver_settings`: la DB manda en runtime. `enabled` gatea la etapa
`asuntos` del pipeline (adhesión + asignador); `adhesion_enabled` permite apagar solo la adhesión
determinista. El proveedor/modelo del asignador los decide el registry por consumer
(`asuntos_assign`), no esta tabla.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class AsuntosSettings:
    """Settings resueltos de asuntos para un usuario.

    `batch_size`: mensajes por llamada del asignador (una ventana grande se parte).
    `body_chars`: recorte del cuerpo (sin citas) que ve el modelo por mensaje.
    `closed_window_days`: los asuntos cerrados hace menos de N días siguen en el catálogo del
    prompt (para adherir rezagados o reabrir). `inactive_days`: sin señal N días → `inactivo`
    (lo aplica el mantenimiento). `min_evidence_open`: evidencias para promover `candidato` →
    `abierto` (una semilla fuerte también promueve). `min_confidence`: piso para aceptar una
    asignación/relación del modelo.
    """

    enabled: bool = True
    adhesion_enabled: bool = True
    batch_size: int = 40
    body_chars: int = 900
    closed_window_days: int = 60
    inactive_days: int = 60
    min_evidence_open: int = 2
    min_confidence: float = 0.6


_COLS = tuple(f.name for f in fields(AsuntosSettings))


def get_settings(conn: Connection, user_id: int) -> AsuntosSettings:
    """Settings del usuario; sin fila → defaults."""
    row = (
        conn.execute(
            text(f"SELECT {', '.join(_COLS)} FROM asuntos_settings WHERE user_id = :uid"),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return AsuntosSettings()
    return AsuntosSettings(
        enabled=bool(row["enabled"]),
        adhesion_enabled=bool(row["adhesion_enabled"]),
        batch_size=int(row["batch_size"]),
        body_chars=int(row["body_chars"]),
        closed_window_days=int(row["closed_window_days"]),
        inactive_days=int(row["inactive_days"]),
        min_evidence_open=int(row["min_evidence_open"]),
        min_confidence=float(row["min_confidence"]),
    )


def upsert_settings(conn: Connection, user_id: int, **changes: Any) -> AsuntosSettings:
    """Upsert PARCIAL (solo los campos pasados); devuelve los settings resultantes.

    Valida en capa de aplicación (accionable para API/CLI) además del CHECK de la DB.
    """
    unknown = set(changes) - set(_COLS)
    if unknown:
        raise ValueError(f"campos desconocidos: {sorted(unknown)}")
    provided = {k: v for k, v in changes.items() if v is not None}
    if "min_confidence" in provided and not (0.0 <= float(provided["min_confidence"]) <= 1.0):
        raise ValueError(f"min_confidence inválido: {provided['min_confidence']} (rango 0..1)")
    for name, lo, hi in (
        ("batch_size", 1, 200),
        ("body_chars", 200, 8000),
        ("closed_window_days", 0, 10_000),
        ("inactive_days", 1, 10_000),
        ("min_evidence_open", 1, 100),
    ):
        if name in provided and not (lo <= int(provided[name]) <= hi):
            raise ValueError(f"{name} inválido: {provided[name]} (rango {lo}..{hi})")
    resolved = replace(get_settings(conn, user_id), **provided)
    conn.execute(
        text(
            f"""
            INSERT INTO asuntos_settings (user_id, {", ".join(_COLS)})
            VALUES (:uid, {", ".join(":" + c for c in _COLS)})
            ON CONFLICT (user_id) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS)},
                updated_at = NOW()
            """
        ),
        {"uid": user_id, **{c: getattr(resolved, c) for c in _COLS}},
    )
    return resolved
