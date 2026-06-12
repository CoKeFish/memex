"""Carga del work-set de extracción: mensajes clasificados ORIGINALES pendientes (ADR-015 §9).

Trackea progreso por la AUSENCIA de fila en `module_extractions` (cursor por (module_slug,
inbox_id)), igual que el summarizer con `summary_inbox_links`. El work-set es la UNIÓN de los
pendientes de cada módulo activo: un mensaje entra si AL MENOS UN módulo activo consume su
categoría (`consumes_kinds`) y todavía no tiene cursor para ese módulo. Los mensajes de una
categoría que ningún módulo activo consume no se cargan (no hay nada que hacer con ellos).

Excluir los ya-hechos en SQL (NOT EXISTS por módulo) evita el "stall" de cargar siempre las
mismas filas ya procesadas. El LIMIT corta a nivel de MENSAJE (igual que el summarizer):
ineficiencia entre corridas, no incorrectitud (la idempotencia del cursor lo cubre).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.deadletter import STAGE_EXTRACT, not_in_review_sql
from memex.core.media import MAX_OCR_ATTEMPTS, MEDIA_NOT_TERMINAL_SQL
from memex.logging import get_logger
from memex.modules.contract import InterestModule
from memex.processing.windows import WorkRow
from memex.relevance.verdicts import workset_gate_clause
from memex.sources import kind_for_type, kind_types

_log = get_logger("memex.modules.workset")


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _types_for_module(module: InterestModule) -> list[str]:
    """Tipos de source (sources.type) cuyas categorías el módulo consume. Enumera por
    `kind_types()` (todos los tipos con categoría), no por `known_types()` (solo pulleables),
    para incluir fuentes push-only como outlook."""
    return [t for t in kind_types() if kind_for_type(t) in module.consumes_kinds]


def load_module_workset(
    conn: Connection,
    user_id: int,
    *,
    source_id: int | None,
    modules: list[InterestModule],
    limit: int,
    inbox_ids: list[int] | None = None,
) -> list[WorkRow]:
    """Mensajes clasificados pendientes para AL MENOS UN módulo activo (ver módulo docstring).

    `inbox_ids` acota a un set explícito (reproceso por lote): conserva el filtro de tier/gates,
    así blacklist se sigue saltando (igual que el daemon).
    """
    clauses: list[str] = []
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": limit,
        "ocrmax": MAX_OCR_ATTEMPTS,
        "dl_stage": STAGE_EXTRACT,
    }
    for idx, module in enumerate(modules):
        types = _types_for_module(module)
        if not types:
            continue
        clauses.append(
            f"(s.type = ANY(:types_{idx}) AND NOT EXISTS ("
            f"  SELECT 1 FROM module_extractions me "
            f"  WHERE me.inbox_id = i.id AND me.module_slug = :slug_{idx}))"
        )
        params[f"types_{idx}"] = types
        params[f"slug_{idx}"] = module.slug

    if not clauses:
        return []

    source_filter = ""
    if source_id is not None:
        source_filter = "AND i.source_id = :sid"
        params["sid"] = source_id

    inbox_filter = ""
    if inbox_ids is not None:
        inbox_filter = "AND i.id = ANY(:iids)"
        params["iids"] = inbox_ids

    # Gate de relevancia (correos): encendido, un correo sin relevancia efectiva (mark manual
    # o veredicto `relevant`) no entra al workset; apagado → cláusula vacía.
    gate_clause, gate_params = workset_gate_clause(conn, user_id)
    params.update(gate_params)

    pending_or = " OR ".join(clauses)
    rows = (
        conn.execute(
            text(
                f"""
                SELECT i.id, i.source_id, i.occurred_at, i.payload, c.tier, s.type AS source_type,
                       COALESCE(ma.ocr_text, '') AS ocr_text
                FROM classifications c
                JOIN inbox i   ON i.id = c.inbox_id
                JOIN sources s ON s.id = i.source_id
                LEFT JOIN (
                    SELECT inbox_id, string_agg(ocr_text, E'\n' ORDER BY id) AS ocr_text
                    FROM media_assets
                    WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                    GROUP BY inbox_id
                ) ma ON ma.inbox_id = i.id
                WHERE c.user_id = :uid
                  AND c.tier IN ('batch', 'individual')
                  AND NOT EXISTS (
                      SELECT 1 FROM media_assets m
                      WHERE m.inbox_id = i.id AND {MEDIA_NOT_TERMINAL_SQL}
                  )
                  AND {not_in_review_sql("i.id")}
                  {gate_clause}
                  {source_filter}
                  {inbox_filter}
                  AND ({pending_or})
                ORDER BY i.source_id, i.occurred_at
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )

    return [
        WorkRow(
            inbox_id=int(r["id"]),
            source_id=int(r["source_id"]),
            occurred_at=r["occurred_at"],
            payload=_coerce_payload(r["payload"]),
            tier=str(r["tier"]),
            source_type=str(r["source_type"]),
            ocr_text=str(r["ocr_text"]),
        )
        for r in rows
    ]
