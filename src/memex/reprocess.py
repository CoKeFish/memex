"""Reproceso por mensaje: re-aplicar etapas del pipeline a inbox YA ingeridos.

Un punto único que comparten el endpoint (`POST /inbox/{id}/reprocess`) y el CLI `memex-reprocess`.
Las etapas corren en orden de dependencia (`STAGE_ORDER`): `media` + `ocr` alimentan el texto que
`summarize`/`extract` consumen, y `classify` precede a ambos. Cada etapa
DELEGA en la función que ya existe (no reimplementa): backfill, run_ocr, classify, summarize_inbox,
extract_inbox. Best-effort por etapa: una que falla se loguea y se reporta en su slot del resultado,
sin frenar las demás (un reproceso multi-etapa con éxito parcial es normal).

Sin scheduler ni colas (ADR / decisión `no-background-processing-default`): el endpoint corre
síncrono por-mensaje; los lotes (por fuente / rango / filtro) van por el CLI, vigilados.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text

from memex.classifier.rules import classify
from memex.db import connection
from memex.logging import bound_log_context, get_logger
from memex.media_backfill import backfill_inbox_media
from memex.modules.orchestrator import extract_inbox, run_extraction
from memex.ocr.worker import run_ocr
from memex.relevance.gate import run_relevance_gate
from memex.summarizer.worker import run_summarization, summarize_inbox

_log = get_logger("memex.reprocess")

#: Orden de dependencia de las etapas. Se corren SIEMPRE en este orden, sea cual sea el set elegido.
#: `relevance` (gate de intereses, solo correos) corre tras classify y ANTES de cualquier LLM:
#: con el gate encendido, summarize/extract excluyen lo no-relevante (apagado → no-op).
STAGE_ORDER: tuple[str, ...] = ("media", "ocr", "classify", "relevance", "summarize", "extract")
VALID_STAGES = frozenset(STAGE_ORDER)
#: Predicados de selección acotados (no especulativos): adjuntos declarados sin media / con error.
ONLY_FILTERS = frozenset({"unstored-attachments", "errored"})


def select_targets(
    user_id: int,
    *,
    inbox_ids: list[int] | None = None,
    source_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    only: str | None = None,
    order: Literal["id", "occurred_at"] = "id",
) -> list[int]:
    """Resuelve los inbox_id objetivo de un reproceso por lote (CLI). Sin filtros → todos.

    `order="occurred_at"` (cronológico, `id` de desempate) es para el snapshot del lote por
    ventanas: ahí "los próximos N" significa "los N más viejos", no orden de inserción.
    """
    if only is not None and only not in ONLY_FILTERS:
        raise ValueError(f"only inválido: {only!r}; válidos: {sorted(ONLY_FILTERS)}")
    if order not in ("id", "occurred_at"):
        raise ValueError(f"order inválido: {order!r}")
    where = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if inbox_ids:
        where.append("i.id = ANY(:iids)")
        params["iids"] = inbox_ids
    if source_id is not None:
        where.append("i.source_id = :sid")
        params["sid"] = source_id
    if since is not None:
        where.append("i.occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("i.occurred_at < :until")
        params["until"] = until
    if only == "unstored-attachments":
        where.append(
            "jsonb_typeof(i.payload->'attachments') = 'array' "
            "AND jsonb_array_length(i.payload->'attachments') > 0 "
            "AND NOT EXISTS (SELECT 1 FROM media_assets m WHERE m.inbox_id = i.id)"
        )
    elif only == "errored":
        where.append(
            "(i.process_error IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM media_assets m WHERE m.inbox_id = i.id "
            "AND m.ocr_status = 'error'))"
        )
    order_sql = "i.occurred_at, i.id" if order == "occurred_at" else "i.id"
    sql = f"SELECT i.id FROM inbox i WHERE {' AND '.join(where)} ORDER BY {order_sql}"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    with connection() as conn:
        return [int(r[0]) for r in conn.execute(text(sql), params)]


def _classify_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    """Clasifica (determinista) cada inbox. `force` re-clasifica (borra la fila previa).

    Paridad con el worker standalone (`classifier.run.end`): devuelve el desglose `by_tier`
    (misma forma que sus stats) y emite `reprocess.classify.done` — así cada corrida de lote
    también cuenta en /logs cuántos mensajes cayeron a cada tier, no solo el total."""
    classified = already = missing = 0
    by_tier: dict[str, int] = {}
    with connection() as conn:
        for iid in inbox_ids:
            payload = conn.execute(
                text("SELECT payload FROM inbox WHERE id = :id AND user_id = :uid"),
                {"id": iid, "uid": user_id},
            ).scalar()
            if payload is None:
                missing += 1
                continue
            if force:
                conn.execute(text("DELETE FROM classifications WHERE inbox_id = :id"), {"id": iid})
            elif conn.execute(
                text("SELECT 1 FROM classifications WHERE inbox_id = :id"), {"id": iid}
            ).scalar():
                already += 1
                continue
            result = classify(payload if isinstance(payload, dict) else {})
            conn.execute(
                text(
                    "INSERT INTO classifications (user_id, inbox_id, tier, metadata) "
                    "VALUES (:u, :i, :t, CAST(:m AS JSONB)) ON CONFLICT (inbox_id) DO NOTHING"
                ),
                {"u": user_id, "i": iid, "t": result.tier, "m": json.dumps(result.metadata)},
            )
            classified += 1
            by_tier[result.tier] = by_tier.get(result.tier, 0) + 1
    _log.info(
        "reprocess.classify.done",
        n=len(inbox_ids),
        classified=classified,
        already=already,
        missing=missing,
        **{f"tier_{tier}": count for tier, count in by_tier.items()},
    )
    return {"classified": classified, "already": already, "missing": missing, "by_tier": by_tier}


async def _relevance_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    """Corre el gate de relevancia sobre los targets (apagado → no-op con stats en cero).

    `force` borra primero los veredictos NO manuales de esos ids (re-juzga). A diferencia de
    summarize/extract no hay vía per-mensaje especial: el gate ventanea igual con 1 que con N.
    """
    stats = await run_relevance_gate(user_id, inbox_ids=inbox_ids, force=force)
    return {
        "ok": stats.messages,
        "messages": stats.messages,
        "relevant": stats.relevant,
        "not_relevant": stats.not_relevant,
        "insufficient": stats.insufficient,
        "by_rule": stats.by_rule,
        "skipped": stats.skipped,
        "errors": stats.errors,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


async def _summarize_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    """Resume respetando los tiers, igual que el daemon (salta blacklist, ventanea batch).

    Lote (>1) → `run_summarization(inbox_ids=...)`: un solo cliente LLM para todo el lote. Un solo
    mensaje (botón "Reprocesar" de /datos/:id) → vía per-mensaje, que honra el click explícito y
    refresca su resumen aunque sea blacklist.
    """
    if len(inbox_ids) == 1:
        try:
            r = await summarize_inbox(user_id, inbox_ids[0], scope="individual", force=force)
            return {"ok": 1, "errors": 0, "cost_usd": float(r.get("cost_usd", 0) or 0)}
        except Exception as e:  # best-effort (no clasificado, sin LLM, etc.)
            _log.warning("reprocess.summarize_failed", inbox_id=inbox_ids[0], error=str(e))
            return {"ok": 0, "errors": 1, "cost_usd": 0.0}
    stats = await run_summarization(user_id, inbox_ids=inbox_ids, force=force)
    return {
        "ok": stats.summaries,
        "summaries": stats.summaries,
        "messages": stats.messages,
        "skipped": stats.skipped,
        "errors": stats.errors,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


async def _extract_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    """Extrae respetando los tiers, igual que el daemon (salta blacklist, ventanea batch).

    Lote (>1) → `run_extraction(inbox_ids=...)`: un solo cliente LLM. Un solo mensaje → vía
    per-mensaje (`extract_inbox`), que honra el click explícito y preserva la traza de /datos/:id.
    """
    if len(inbox_ids) == 1:
        try:
            r = await extract_inbox(user_id, inbox_ids[0], scope="individual", force=force)
            return {
                "ok": 1,
                "errors": 0,
                "items": int(r.get("items", 0) or 0),
                "cost_usd": float(r.get("cost_usd", 0) or 0),
            }
        except Exception as e:  # best-effort
            _log.warning("reprocess.extract_failed", inbox_id=inbox_ids[0], error=str(e))
            return {"ok": 0, "errors": 1, "items": 0, "cost_usd": 0.0}
    stats = await run_extraction(user_id, inbox_ids=inbox_ids, force=force)
    return {
        "ok": stats.windows,
        "items": stats.items,
        "windows": stats.windows,
        "discarded": stats.discarded,
        "errors": stats.errors,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


async def reprocess(
    user_id: int, *, stages: list[str], targets: list[int], force: bool = False
) -> dict[str, Any]:
    """Corre `stages` (en `STAGE_ORDER`) sobre `targets`. Devuelve el resultado por etapa.

    Correlación: con UN solo target se bindea `inbox_id` a contextvars por el scope de la corrida
    (los lotes de a 1 y el camino individual quedan filtrables por `/logs?inbox_id=`); con varios,
    la atribución por mensaje la dan los eventos que llevan `inbox_id`/`inbox_ids` explícitos
    (OCR por asset, unidades del orquestador)."""
    invalid = [s for s in stages if s not in VALID_STAGES]
    if invalid:
        raise ValueError(f"stages inválidas: {invalid}; válidas: {sorted(VALID_STAGES)}")
    ordered = [s for s in STAGE_ORDER if s in set(stages)]
    per_stage: dict[str, Any] = {}
    out: dict[str, Any] = {"targets": len(targets), "stages": ordered, "results": per_stage}
    if not targets:
        return out

    with bound_log_context(inbox_id=targets[0] if len(targets) == 1 else None):
        return await _run_stages(user_id, ordered, targets, force, per_stage, out)


async def _run_stages(
    user_id: int,
    ordered: list[str],
    targets: list[int],
    force: bool,
    per_stage: dict[str, Any],
    out: dict[str, Any],
) -> dict[str, Any]:
    for stage in ordered:
        try:
            if stage == "media":
                per_stage["media"] = asdict(
                    await asyncio.to_thread(backfill_inbox_media, user_id, targets)
                )
            elif stage == "ocr":
                s = await run_ocr(user_id, inbox_ids=targets, reocr=force)
                per_stage["ocr"] = {
                    "ok": s.ok,
                    "deduped": s.deduped,
                    "truncated": s.truncated,
                    "errors": s.errors,
                    "cost_usd": float(s.cost.total.cost_usd),
                }
            elif stage == "classify":
                per_stage["classify"] = await asyncio.to_thread(
                    _classify_targets, user_id, targets, force
                )
            elif stage == "relevance":
                per_stage["relevance"] = await _relevance_targets(user_id, targets, force)
            elif stage == "summarize":
                per_stage["summarize"] = await _summarize_targets(user_id, targets, force)
            elif stage == "extract":
                per_stage["extract"] = await _extract_targets(user_id, targets, force)
        except Exception as e:  # una etapa que falla no frena las demás; se reporta en su slot
            per_stage[stage] = {"error": str(e)}
            _log.error(
                "reprocess.stage_failed", stage=stage, exc_type=type(e).__name__, exc_msg=str(e)
            )
    # Costo total de la corrida = suma de las etapas que reportan `cost_usd` (una etapa fallada es
    # `{"error": ...}` y suma 0). Redondeado: la suma de floats arrastra ruido binario.
    out["cost_usd"] = round(
        sum(float(r.get("cost_usd", 0) or 0) for r in per_stage.values() if isinstance(r, dict)), 6
    )
    _log.info(
        "reprocess.done",
        user_id=user_id,
        targets=len(targets),
        stages=ordered,
        cost_usd=out["cost_usd"],
    )
    return out
