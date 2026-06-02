"""Procesamiento desde /procesamiento: corridas por lote + control del scheduler.

Dos superficies:

1. **Corridas por lote** (`/processing/dry-run`, `/run`, `/runs[/{id}]`). El usuario elige QUÉ
   procesar con los mismos filtros del CLI `memex-reprocess` (etapas + fuente + rango de fechas +
   cantidad + `only` + `force`). `dry-run` resuelve los objetivos con `select_targets()` sin
   escribir; `run` encola una corrida que corre EN BACKGROUND dentro del proceso API
   (`asyncio.create_task`, igual que `reprocess()` que ya usa `asyncio.to_thread` para lo pesado) y
   deja rastro en `worker_runs` (`run_type='reprocess'`). La UI hace polling de `/runs`. Una corrida
   a la vez (409 si hay otra `running`) — coherente con "procesamiento vigilado".

2. **Control runtime del scheduler** (`/processing/scheduler`). GET combina la config de DB
   (`scheduler_settings`) con el último run real de cada job (`worker_runs`, `run_type='job'`).
   PATCH prende/apaga el daemon y setea qué jobs corren; el daemon relee la DB cada tick. Off por
   default.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from memex.api.auth import current_user_id
from memex.api.schemas import (
    ProcessingDryRun,
    ProcessingRunList,
    ProcessingRunRequest,
    ProcessingRunRow,
    ProcessingRunStatus,
    SchedulerJobState,
    SchedulerSettingsPatch,
    SchedulerState,
)
from memex.db import connection
from memex.logging import get_logger
from memex.reprocess import STAGE_ORDER, reprocess, select_targets
from memex.scheduler import runs
from memex.scheduler.jobs import all_jobs

router = APIRouter(prefix="/processing", tags=["processing"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.processing")

#: Mismo umbral de "colgado" que stats.py (último run 'running' pasado este tiempo).
_STALE = "interval '30 minutes'"

#: Referencias vivas a las corridas en background (sin esto el GC podría matar la task).
_RUNNING: set[asyncio.Task[Any]] = set()


# --- helpers ---
def _to_dt(d: date | None) -> datetime | None:
    """Fecha (YYYY-MM-DD) → datetime UTC a medianoche (igual que `memex-reprocess`)."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC) if d is not None else None


def _resolve_targets(user_id: int, body: ProcessingRunRequest) -> list[int]:
    return select_targets(
        user_id,
        source_id=body.source_id,
        since=_to_dt(body.since),
        until=_to_dt(body.until),
        limit=body.limit,
        only=body.only,
    )


def _ordered_stages(stages: Sequence[str]) -> list[str]:
    """Reordena al orden de dependencia (igual que `reprocess()`), sea cual sea el orden dado."""
    chosen = set(stages)
    return [s for s in STAGE_ORDER if s in chosen]


async def _run_batch(
    user_id: int, run_id: int, stages: list[str], targets: list[int], force: bool
) -> None:
    """Corre `reprocess()` y cierra la fila de `worker_runs` (ok/error). Corre en background."""
    log = _log.bind(run_id=run_id, user_id=user_id)
    try:
        result = await reprocess(user_id, stages=stages, targets=targets, force=force)
        runs.finish_run(run_id, status="ok", stats=result)
        log.info("processing.run.done", targets=len(targets), stages=stages)
    except Exception as e:  # best-effort: el error queda en la fila para el post-mortem
        runs.finish_run(run_id, status="error", error=str(e))
        log.error("processing.run.failed", error=str(e), exc_type=type(e).__name__)


def _run_row(r: RowMapping) -> ProcessingRunRow:
    return ProcessingRunRow(
        id=int(r["id"]),
        status=str(r["status"]),
        stats=dict(r["stats"] or {}),
        error=r["error"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        run_config=dict(r["run_config"] or {}),
        is_stale=bool(r["is_stale"]),
    )


# --- corridas por lote ---
@router.post("/dry-run", response_model=ProcessingDryRun)
async def dry_run(body: ProcessingRunRequest, user_id: UserID) -> dict[str, Any]:
    """Previa sin escribir: cuántos mensajes caen bajo el filtro + una muestra de ids."""
    if not body.stages:
        raise HTTPException(status_code=422, detail="elegí al menos una etapa")
    targets = _resolve_targets(user_id, body)
    return {
        "count": len(targets),
        "sample_ids": targets[:50],
        "stages": _ordered_stages(body.stages),
    }


@router.post("/run", response_model=ProcessingRunStatus)
async def run_batch(body: ProcessingRunRequest, user_id: UserID) -> dict[str, Any]:
    """Encola una corrida por lote en background. Devuelve el `run_id` para poll-ear `/runs`."""
    if not body.stages:
        raise HTTPException(status_code=422, detail="elegí al menos una etapa")
    ordered = _ordered_stages(body.stages)

    with connection() as conn:
        busy = conn.execute(
            text(
                "SELECT 1 FROM worker_runs "
                "WHERE user_id = :uid AND run_type = 'reprocess' AND status = 'running' LIMIT 1"
            ),
            {"uid": user_id},
        ).first()
    if busy:
        raise HTTPException(status_code=409, detail="ya hay una corrida de procesamiento en curso")

    targets = _resolve_targets(user_id, body)
    if not targets:
        return {"run_id": None, "status": "empty", "count": 0, "stages": ordered}

    run_id = runs.start_run(user_id, "reprocess")
    cfg = {
        "stages": ordered,
        "targets": targets,
        "force": body.force,
        "filters": {
            "source_id": body.source_id,
            "since": body.since.isoformat() if body.since else None,
            "until": body.until.isoformat() if body.until else None,
            "limit": body.limit,
            "only": body.only,
        },
    }
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE worker_runs SET run_type = 'reprocess', run_config = CAST(:cfg AS JSONB) "
                "WHERE id = :id"
            ),
            {"cfg": json.dumps(cfg), "id": run_id},
        )

    task = asyncio.create_task(_run_batch(user_id, run_id, ordered, targets, body.force))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    _log.info(
        "processing.run.enqueued",
        user_id=user_id,
        run_id=run_id,
        stages=ordered,
        targets=len(targets),
    )
    return {"run_id": run_id, "status": "running", "count": len(targets), "stages": ordered}


@router.get("/runs", response_model=ProcessingRunList)
async def list_runs(
    user_id: UserID, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> dict[str, Any]:
    """Corridas por lote recientes (run_type='reprocess'), más nuevas primero. La UI poll-ea acá."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT id, status, stats, error, started_at, finished_at, run_config,
                           (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
                    FROM worker_runs
                    WHERE user_id = :uid AND run_type = 'reprocess'
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """
                ),
                {"uid": user_id, "limit": limit},
            )
            .mappings()
            .all()
        )
    return {"items": [_run_row(r) for r in rows]}


@router.get("/runs/{run_id}", response_model=ProcessingRunRow)
async def get_run(run_id: int, user_id: UserID) -> ProcessingRunRow:
    """Estado de una corrida puntual (polling fino del progreso)."""
    with connection() as conn:
        r = (
            conn.execute(
                text(
                    f"""
                    SELECT id, status, stats, error, started_at, finished_at, run_config,
                           (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
                    FROM worker_runs
                    WHERE id = :id AND user_id = :uid AND run_type = 'reprocess'
                    """
                ),
                {"id": run_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    return _run_row(r)


# --- control runtime del scheduler ---
@router.get("/scheduler", response_model=SchedulerState)
async def get_scheduler(user_id: UserID) -> dict[str, Any]:
    """Estado del scheduler: config de DB + último run real de cada job (read-only)."""
    registry = all_jobs()
    with connection() as conn:
        srow = (
            conn.execute(
                text(
                    "SELECT daemon_enabled, enabled_jobs "
                    "FROM scheduler_settings WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
            .mappings()
            .first()
        )
        worker_rows = {
            r["job"]: r
            for r in conn.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (job)
                           job, started_at, finished_at, status, stats, error,
                           (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
                    FROM worker_runs
                    WHERE user_id = :uid AND run_type = 'job'
                    ORDER BY job, started_at DESC
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        }

    daemon_enabled = bool(srow["daemon_enabled"]) if srow else False
    enabled_csv = str(srow["enabled_jobs"]) if srow else ""
    enabled_set = {j.strip() for j in enabled_csv.split(",") if j.strip()}

    jobs: list[SchedulerJobState] = []
    for name, job in registry.items():
        w = worker_rows.get(name)
        latest = (
            {
                "started_at": w["started_at"],
                "finished_at": w["finished_at"],
                "status": w["status"],
                "stats": w["stats"],
                "error": w["error"],
            }
            if w
            else None
        )
        jobs.append(
            SchedulerJobState(
                name=name,
                default_interval=job.default_interval,
                enabled=name in enabled_set,
                latest=latest,
                is_stale=bool(w["is_stale"]) if w else False,
            )
        )
    return {"daemon_enabled": daemon_enabled, "enabled_jobs": sorted(enabled_set), "jobs": jobs}


@router.patch("/scheduler", response_model=SchedulerState)
async def patch_scheduler(body: SchedulerSettingsPatch, user_id: UserID) -> dict[str, Any]:
    """Prende/apaga el daemon y/o setea los jobs habilitados (CSV). El daemon relee cada tick."""
    fields = body.model_dump(exclude_unset=True)
    if fields.get("enabled_jobs"):
        valid = set(all_jobs())
        bad = sorted(
            j.strip()
            for j in str(fields["enabled_jobs"]).split(",")
            if j.strip() and j.strip() not in valid
        )
        if bad:
            raise HTTPException(
                status_code=422, detail=f"jobs desconocidos: {bad}; válidos: {sorted(valid)}"
            )
    if fields:
        params = {
            "uid": user_id,
            "de": fields.get("daemon_enabled", False),
            "ej": fields.get("enabled_jobs", ""),
        }
        sets: list[str] = []
        if "daemon_enabled" in fields:
            sets.append("daemon_enabled = :de")
        if "enabled_jobs" in fields:
            sets.append("enabled_jobs = :ej")
        sets.append("updated_at = NOW()")
        with connection() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO scheduler_settings (user_id, daemon_enabled, enabled_jobs)
                    VALUES (:uid, :de, :ej)
                    ON CONFLICT (user_id) DO UPDATE SET {", ".join(sets)}
                    """
                ),
                params,
            )
        _log.info("processing.scheduler.patched", user_id=user_id, fields=list(fields.keys()))
    return await get_scheduler(user_id)
