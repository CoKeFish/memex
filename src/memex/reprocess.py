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
from typing import Any

from sqlalchemy import text

from memex.classifier.rules import classify
from memex.db import connection
from memex.logging import get_logger
from memex.media_backfill import backfill_inbox_media
from memex.modules.orchestrator import extract_inbox
from memex.ocr.worker import run_ocr
from memex.summarizer.worker import summarize_inbox

_log = get_logger("memex.reprocess")

#: Orden de dependencia de las etapas. Se corren SIEMPRE en este orden, sea cual sea el set elegido.
STAGE_ORDER: tuple[str, ...] = ("media", "ocr", "classify", "summarize", "extract")
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
) -> list[int]:
    """Resuelve los inbox_id objetivo de un reproceso por lote (CLI). Sin filtros → todos."""
    if only is not None and only not in ONLY_FILTERS:
        raise ValueError(f"only inválido: {only!r}; válidos: {sorted(ONLY_FILTERS)}")
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
    sql = f"SELECT i.id FROM inbox i WHERE {' AND '.join(where)} ORDER BY i.id"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    with connection() as conn:
        return [int(r[0]) for r in conn.execute(text(sql), params)]


def _classify_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, int]:
    """Clasifica (determinista) cada inbox. `force` re-clasifica (borra la fila previa)."""
    classified = already = missing = 0
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
    return {"classified": classified, "already": already, "missing": missing}


async def _summarize_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    ok = errors = 0
    cost = 0.0
    for iid in inbox_ids:
        try:
            r = await summarize_inbox(user_id, iid, scope="individual", force=force)
            ok += 1
            cost += float(r.get("cost_usd", 0) or 0)
        except Exception as e:  # best-effort por mensaje (no clasificado, sin LLM, etc.)
            errors += 1
            _log.warning("reprocess.summarize_failed", inbox_id=iid, error=str(e))
    return {"ok": ok, "errors": errors, "cost_usd": cost}


async def _extract_targets(user_id: int, inbox_ids: list[int], force: bool) -> dict[str, Any]:
    ok = errors = items = 0
    cost = 0.0
    for iid in inbox_ids:
        try:
            r = await extract_inbox(user_id, iid, scope="individual", force=force)
            ok += 1
            items += int(r.get("items", 0) or 0)
            cost += float(r.get("cost_usd", 0) or 0)
        except Exception as e:  # best-effort por mensaje
            errors += 1
            _log.warning("reprocess.extract_failed", inbox_id=iid, error=str(e))
    return {"ok": ok, "errors": errors, "items": items, "cost_usd": cost}


async def reprocess(
    user_id: int, *, stages: list[str], targets: list[int], force: bool = False
) -> dict[str, Any]:
    """Corre `stages` (en `STAGE_ORDER`) sobre `targets`. Devuelve el resultado por etapa."""
    invalid = [s for s in stages if s not in VALID_STAGES]
    if invalid:
        raise ValueError(f"stages inválidas: {invalid}; válidas: {sorted(VALID_STAGES)}")
    ordered = [s for s in STAGE_ORDER if s in set(stages)]
    per_stage: dict[str, Any] = {}
    out: dict[str, Any] = {"targets": len(targets), "stages": ordered, "results": per_stage}
    if not targets:
        return out

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
                }
            elif stage == "classify":
                per_stage["classify"] = await asyncio.to_thread(
                    _classify_targets, user_id, targets, force
                )
            elif stage == "summarize":
                per_stage["summarize"] = await _summarize_targets(user_id, targets, force)
            elif stage == "extract":
                per_stage["extract"] = await _extract_targets(user_id, targets, force)
        except Exception as e:  # una etapa que falla no frena las demás; se reporta en su slot
            per_stage[stage] = {"error": str(e)}
            _log.error(
                "reprocess.stage_failed", stage=stage, exc_type=type(e).__name__, exc_msg=str(e)
            )
    _log.info("reprocess.done", user_id=user_id, targets=len(targets), stages=ordered)
    return out
