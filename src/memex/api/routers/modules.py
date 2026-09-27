"""Módulos de extracción para /procesamiento: estado (toggle + perillas) + cobertura.

Solo expone los slugs resolubles del registry (`known_modules()`: finance, calendar, hackathones,
identidades). Para cada
uno combina:
- `module_settings` (LEFT JOIN: `enabled`/`batching_policy`/`group_size`, con defaults coherentes
  con el orquestador si el usuario nunca tocó el módulo), y
- la COBERTURA `processed/total/pending`: cuántos mensajes elegibles ya pasaron por la extracción
  de ese módulo. El denominador (`total`) son los inbox clasificados (tier batch/individual) cuya
  fuente cae en `consumes_kinds` del módulo y cuya media está en estado terminal — el mismo gate que
  usa `load_module_workset` (workset.py), para que la barra refleje lo que el orquestador procesa.

`PATCH /modules/{slug}` hace el mismo UPSERT que el CLI `memex-extract enable` (ON CONFLICT
(user_id, module_slug)) pero parcial: solo toca los campos enviados.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.api.auth import current_user_id
from memex.api.schemas import ModuleList, ModulePatch, ModuleRow
from memex.core.media import MAX_OCR_ATTEMPTS, MEDIA_NOT_TERMINAL_SQL
from memex.db import connection
from memex.logging import get_logger
from memex.modules import known_modules, resolve
from memex.sources import kind_for_type, kind_types

router = APIRouter(prefix="/modules", tags=["modules"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.modules")

#: Etiquetas "lindas" para la UI (alineadas con el mock previo `@/mocks/control`). El módulo solo
#: expone `slug`; el label de presentación vive acá.
_LABELS: dict[str, str] = {
    "finance": "Finanzas",
    "calendar": "Calendario (eventos)",
    "identidades": "Identidades (directorio)",
    "hackathones": "Hackathones (eventos)",
}

#: Defaults coherentes con el esquema (0008/0023) cuando el usuario no tiene fila de settings.
_DEFAULTS = {"enabled": False, "batching_policy": "per_module", "group_size": 3}


def _types_for_slug(slug: str) -> list[str]:
    """Tipos de source (sources.type) cuyas categorías consume el módulo. Espeja el pre-filtro de
    `memex.modules.workset._types_for_module`, pero solo con símbolos públicos."""
    module = resolve(slug)()
    return [t for t in kind_types() if kind_for_type(t) in module.consumes_kinds]


def _coverage(conn: Connection, user_id: int, slug: str) -> tuple[int, int]:
    """`(processed, total)` del módulo: total = elegibles (clasificados + tipo consumido + media
    terminal); processed = los que ya tienen cursor en `module_extractions`."""
    types = _types_for_slug(slug)
    if not types:
        return (0, 0)
    row = (
        conn.execute(
            text(
                f"""
                SELECT
                    COUNT(DISTINCT i.id) AS total,
                    COUNT(DISTINCT i.id) FILTER (WHERE me.inbox_id IS NOT NULL) AS processed
                FROM classifications c
                JOIN inbox i   ON i.id = c.inbox_id
                JOIN sources s ON s.id = i.source_id
                LEFT JOIN module_extractions me
                       ON me.inbox_id = i.id AND me.module_slug = :slug AND me.user_id = :uid
                WHERE c.user_id = :uid
                  AND c.tier IN ('batch', 'individual')
                  AND s.type = ANY(:types)
                  AND NOT EXISTS (
                      SELECT 1 FROM media_assets m
                      WHERE m.inbox_id = i.id AND {MEDIA_NOT_TERMINAL_SQL}
                  )
                """
            ),
            {"uid": user_id, "slug": slug, "types": types, "ocrmax": MAX_OCR_ATTEMPTS},
        )
        .mappings()
        .one()
    )
    return (int(row["processed"]), int(row["total"]))


def _module_row(conn: Connection, user_id: int, slug: str, settings: dict[str, Any]) -> ModuleRow:
    processed, total = _coverage(conn, user_id, slug)
    return ModuleRow(
        slug=slug,
        label=_LABELS.get(slug, slug),
        enabled=bool(settings.get("enabled", _DEFAULTS["enabled"])),
        batching_policy=str(settings.get("batching_policy", _DEFAULTS["batching_policy"])),
        group_size=int(settings.get("group_size", _DEFAULTS["group_size"])),
        processed=processed,
        total=total,
        pending=max(total - processed, 0),
    )


def _settings_by_slug(conn: Connection, user_id: int) -> dict[str, dict[str, Any]]:
    rows = (
        conn.execute(
            text(
                "SELECT module_slug, enabled, batching_policy, group_size "
                "FROM module_settings WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    return {r["module_slug"]: dict(r) for r in rows}


@router.get("", response_model=ModuleList)
async def list_modules(user_id: UserID) -> dict[str, Any]:
    """Lista los módulos conocidos con su estado (enabled/batching/group_size) + cobertura."""
    with connection() as conn:
        settings = _settings_by_slug(conn, user_id)
        items = [
            _module_row(conn, user_id, slug, settings.get(slug, {})) for slug in known_modules()
        ]
    return {"items": items}


@router.patch("/{slug}", response_model=ModuleRow)
async def patch_module(slug: str, body: ModulePatch, user_id: UserID) -> ModuleRow:
    """Edición parcial de un módulo: `enabled` / `batching_policy` / `group_size` (UPSERT)."""
    if slug not in known_modules():
        raise HTTPException(status_code=404, detail=f"módulo desconocido: {slug!r}")
    fields = body.model_dump(exclude_unset=True)
    with connection() as conn:
        if fields:
            params = {
                "uid": user_id,
                "slug": slug,
                "enabled": fields.get("enabled", _DEFAULTS["enabled"]),
                "bp": fields.get("batching_policy", _DEFAULTS["batching_policy"]),
                "gs": fields.get("group_size", _DEFAULTS["group_size"]),
            }
            sets: list[str] = []
            if "enabled" in fields:
                sets.append("enabled = :enabled")
            if "batching_policy" in fields:
                sets.append("batching_policy = :bp")
            if "group_size" in fields:
                sets.append("group_size = :gs")
            update_clause = ", ".join(sets)
            conn.execute(
                text(
                    f"""
                    INSERT INTO module_settings (user_id, module_slug, enabled, batching_policy,
                                                 group_size)
                    VALUES (:uid, :slug, :enabled, :bp, :gs)
                    ON CONFLICT (user_id, module_slug) DO UPDATE SET {update_clause}
                    """
                ),
                params,
            )
        settings = _settings_by_slug(conn, user_id)
        row = _module_row(conn, user_id, slug, settings.get(slug, {}))
    _log.info("modules.patched", user_id=user_id, slug=slug, fields=list(fields.keys()))
    return row
