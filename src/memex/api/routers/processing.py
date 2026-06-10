"""Procesamiento desde /procesamiento: corridas por lote + control del scheduler.

Dos superficies:

1. **Corridas por lote** (`/processing/dry-run`, `/run`, `/runs[/{id}]`). El usuario elige QUÉ
   procesar con los mismos filtros del CLI `memex-reprocess` (etapas + fuente + rango de fechas +
   cantidad + `only` + `force`). `dry-run` resuelve los objetivos con `select_targets()` sin
   escribir; `run` encola una corrida que corre EN BACKGROUND dentro del proceso API
   (`asyncio.create_task`, igual que `reprocess()` que ya usa `asyncio.to_thread` para lo pesado) y
   deja rastro en `worker_runs` (`run_type='reprocess'`). La UI hace polling de `/runs`. Una corrida
   a la vez (409 si hay otra `running`) — coherente con "procesamiento vigilado".

2. **Lote por ventanas** (`/processing/lot[...]`). Para backlogs grandes: congela un snapshot con
   los mismos filtros (orden cronológico) y se avanza en ventanas de N mensajes — N con default
   por medio (`/processing/window-defaults`) — mirando costo por ventana. Cada avance corre en
   background como una corrida más (`worker_runs`); la orquestación vive en
   `memex.processing.lots`. Mismo candado de "una corrida a la vez".

3. **Control runtime del scheduler** (`/processing/scheduler`). GET combina la config de DB
   (`scheduler_settings`) con el último run real de cada job (`worker_runs`, `run_type='job'`).
   PATCH prende/apaga el daemon y setea qué jobs corren; el daemon relee la DB cada tick. Off por
   default.

4. **Cobertura del procesamiento** (`GET /processing/coverage`, SOLO lectura). Espejo de
   `GET /inbox/coverage` pero respondiendo "de lo ingerido, qué ya se digirió": mismo shape
   `CoverageOut`, lanes por fuente, con un `criterion` por etapa (any/summarize/extract).
   Acá no se dispara ningún procesamiento.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from memex.api.auth import current_user_id
from memex.api.coverage_helpers import merge_date_spans, merge_day_buckets
from memex.api.schemas import (
    CoverageOut,
    ProcessingDryRun,
    ProcessingLotAdvance,
    ProcessingLotAdvanceStatus,
    ProcessingLotConfig,
    ProcessingLotState,
    ProcessingRunList,
    ProcessingRunRequest,
    ProcessingRunRow,
    ProcessingRunStatus,
    SchedulerJobState,
    SchedulerSettingsPatch,
    SchedulerState,
    WindowDefaults,
    WindowDefaultsPatch,
)
from memex.db import connection
from memex.logging import get_logger
from memex.processing import lots
from memex.reprocess import STAGE_ORDER, reprocess, select_targets
from memex.scheduler import runs
from memex.scheduler.jobs import all_jobs
from memex.sources import kind_for_type

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
        busy = lots.is_busy(conn, user_id)
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


# --- lote por ventanas ---
_NO_LOT = "no hay lote de procesamiento configurado"
_BUSY = "ya hay una corrida de procesamiento en curso"


def _lot_state(conn: Any, lot: lots.ProcessingLot) -> ProcessingLotState:
    return ProcessingLotState(**lots.to_state(conn, lot))


@router.get("/lot", response_model=ProcessingLotState)
async def get_lot(user_id: UserID) -> ProcessingLotState:
    """Estado del lote para restaurar la UI; 404 si no hay ninguno configurado."""
    with connection() as conn:
        lot = lots.get_lot(conn, user_id)
        if lot is None:
            raise HTTPException(status_code=404, detail=_NO_LOT)
        return _lot_state(conn, lot)


@router.post("/lot", response_model=ProcessingLotState)
async def configure_lot(body: ProcessingLotConfig, user_id: UserID) -> ProcessingLotState:
    """Crea o reconfigura EL lote (frontera 0, history vacío). 409 con una corrida en curso."""
    if not body.stages:
        raise HTTPException(status_code=422, detail="elegí al menos una etapa")
    ordered = _ordered_stages(body.stages)
    targets = select_targets(
        user_id,
        source_id=body.source_id,
        since=_to_dt(body.since),
        until=_to_dt(body.until),
        limit=body.limit,
        only=body.only,
        order="occurred_at",
    )
    if not targets:
        raise HTTPException(status_code=422, detail="el filtro no matchea ningún mensaje")
    filters = {
        "source_id": body.source_id,
        "since": body.since.isoformat() if body.since else None,
        "until": body.until.isoformat() if body.until else None,
        "limit": body.limit,
        "only": body.only,
    }
    with connection() as conn:
        if lots.is_busy(conn, user_id):
            raise HTTPException(status_code=409, detail=_BUSY)
        window_size = lots.resolve_window_size(conn, user_id, targets, body.window_size)
        lot = lots.upsert_lot(
            conn,
            user_id,
            stages=ordered,
            target_ids=targets,
            filters=filters,
            force=body.force,
            window_size=window_size,
        )
        state = _lot_state(conn, lot)
    _log.info(
        "processing.lot.configured",
        user_id=user_id,
        targets=len(targets),
        stages=ordered,
        window_size=window_size,
    )
    return state


@router.delete("/lot", status_code=204)
async def delete_lot(user_id: UserID) -> None:
    """Borra el lote (reset). 409 mientras una corrida lo esté avanzando."""
    with connection() as conn:
        if lots.is_busy(conn, user_id):
            raise HTTPException(status_code=409, detail=_BUSY)
        lots.delete_lot(conn, user_id)
    _log.info("processing.lot.deleted", user_id=user_id)


async def _enqueue_advance(
    user_id: int, *, rest: bool, window_size: int | None
) -> ProcessingLotAdvanceStatus:
    """Lanza el avance en background como una corrida más (`worker_runs`, run_type='reprocess')."""
    with connection() as conn:
        lot = lots.get_lot(conn, user_id)
        if lot is None:
            raise HTTPException(status_code=404, detail=_NO_LOT)
        if lots.is_busy(conn, user_id):
            raise HTTPException(status_code=409, detail=_BUSY)
    total = len(lot.target_ids)
    if lot.frontier >= total:
        return ProcessingLotAdvanceStatus(run_id=None, status="done", window=None)

    size = window_size or lot.window_size
    run_id = runs.start_run(user_id, "reprocess")
    cfg = {
        "stages": lot.stages,
        "force": bool(lot.config.get("force", False)),
        "lot": {
            "mode": "rest" if rest else "window",
            "from": lot.frontier,
            "window_size": size,
            "total": total,
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
    task = asyncio.create_task(
        lots.run_advance(user_id, run_id, rest=rest, window_size=window_size)
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    _log.info(
        "processing.lot.advance_enqueued",
        user_id=user_id,
        run_id=run_id,
        rest=rest,
        from_idx=lot.frontier,
        window_size=size,
    )
    window = (
        None if rest else {"start_idx": lot.frontier, "end_idx": min(lot.frontier + size, total)}
    )
    return ProcessingLotAdvanceStatus(run_id=run_id, status="running", window=window)


@router.post("/lot/advance", response_model=ProcessingLotAdvanceStatus)
async def advance_lot(
    user_id: UserID, body: ProcessingLotAdvance | None = None
) -> ProcessingLotAdvanceStatus:
    """Procesa la PRÓXIMA ventana (el override de tamaño queda como nuevo default del lote)."""
    ov = body or ProcessingLotAdvance()
    return await _enqueue_advance(user_id, rest=False, window_size=ov.window_size)


@router.post("/lot/advance-rest", response_model=ProcessingLotAdvanceStatus)
async def advance_lot_rest(
    user_id: UserID, body: ProcessingLotAdvance | None = None
) -> ProcessingLotAdvanceStatus:
    """Procesa todo lo que queda, ventana a ventana (avance visible y reanudable)."""
    ov = body or ProcessingLotAdvance()
    return await _enqueue_advance(user_id, rest=True, window_size=ov.window_size)


@router.get("/window-defaults", response_model=WindowDefaults)
async def get_window_defaults(user_id: UserID) -> WindowDefaults:
    """Tamaño de ventana por medio (para prellenar el form de alta sin lote configurado)."""
    with connection() as conn:
        return WindowDefaults(sizes=lots.window_defaults(conn, user_id))


@router.patch("/window-defaults", response_model=WindowDefaults)
async def patch_window_defaults(body: WindowDefaultsPatch, user_id: UserID) -> WindowDefaults:
    """Edita los defaults por medio (solo los kinds enviados)."""
    with connection() as conn:
        sizes = lots.set_window_defaults(conn, user_id, body.sizes)
    _log.info("processing.window_defaults.patched", user_id=user_id, sizes=body.sizes)
    return WindowDefaults(sizes=sizes)


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


# --- cobertura del procesamiento (solo lectura) ---
# NOTA: este router no tiene un `GET /{param}` en la raíz, así que `/coverage` no compite con
# nada; si algún día se agrega una ruta paramétrica, esta debe quedar declarada ANTES (la trampa
# de FastAPI documentada en inbox.py: "coverage" se intentaría parsear como el parámetro).

#: TZ por defecto del bucket de cobertura (cuando el cliente no manda `tz`), patrón de inbox.py.
_BUCKET_TZ = "America/Bogota"


def _resolve_tz(tz: str | None) -> str:
    """Valida/resuelve la TZ del bucket. None → `_BUCKET_TZ`; nombre IANA inválido → 422.

    Helper copiado inline a propósito, como en inbox.py/logs.py/metrics.py (convención del repo).
    """
    if tz is None:
        return _BUCKET_TZ
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"timezone inválida: {tz}") from exc
    return tz


#: Fragmentos SQL de "manejado" (constantes propias, nunca input del usuario). Un mensaje está
#: manejado cuando hay DECISIÓN tomada: resumido, extraído, o clasificado blacklist (decisión
#: deliberada de NO procesarlo — por eso blacklist cuenta bajo TODOS los criterios: un
#: blacklisteado jamás va a pasar por la etapa y no debe quedar como "pendiente" eterno).
_SUMMARIZED = "EXISTS (SELECT 1 FROM summary_inbox_links sl WHERE sl.inbox_id = i.id)"
_EXTRACTED = "EXISTS (SELECT 1 FROM module_extractions me WHERE me.inbox_id = i.id)"
_BLACKLISTED = "c.tier = 'blacklist'"
_HANDLED_SQL: dict[str, str] = {
    "any": f"{_SUMMARIZED} OR {_EXTRACTED} OR {_BLACKLISTED}",
    "summarize": f"{_SUMMARIZED} OR {_BLACKLISTED}",
    "extract": f"{_EXTRACTED} OR {_BLACKLISTED}",
}


@router.get("/coverage", response_model=CoverageOut)
async def processing_coverage(
    user_id: UserID,
    tz: str | None = None,
    gap_days: Annotated[int, Query(ge=0, le=365)] = 2,
    source_id: int | None = None,
    kind: Literal["email", "chat", "social", "other"] | None = None,
    since: date | None = None,
    until: date | None = None,
    criterion: Literal["any", "summarize", "extract"] = "any",
) -> dict[str, Any]:
    """Cobertura del PROCESAMIENTO sobre lo ya ingerido, por fuente (espejo de /inbox/coverage).

    El denominador es lo INGERIDO: cada día (en la tz pedida) se pinta según cuántos de sus
    mensajes están MANEJADOS bajo `criterion` (`any` = resumido/extraído/blacklist;
    `summarize`/`extract` = avance de esa etapa, blacklist incluido):

    - `ranges` (banda sólida): días donde TODOS los mensajes del día están manejados. La fusión
      por `gap_days` solo puentea días SIN mensajes — un día con pendientes corta el tramo,
      la banda sólida nunca tapa un pendiente.
    - `swept` (banda tenue): días PARCIALES (algunos manejados, otros no); adyacentes se funden.
    - `cursor` (marcador): frontera del lote de `processing_lots` — el `occurred_at` del último
      mensaje ya procesado por el lote ("el lote va por acá"). Si el lote se creó filtrado a una
      fuente va solo en esa lane; si no, en todas (el snapshot ordena cronológicamente ENTRE
      fuentes: la frontera es una posición temporal global). Omitido fuera de la ventana.

    `total` por lane = mensajes manejados en la ventana (días completos + parciales). Un día sin
    nada manejado no se pinta: el hueco es backlog (lo ingerido se ve en /inbox/coverage).
    """
    resolved_tz = _resolve_tz(tz)
    if since is not None and until is not None and until < since:
        raise HTTPException(status_code=422, detail="until no puede ser anterior a since")

    src_where = ["user_id = :uid"]
    bucket_where = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if source_id is not None:
        src_where.append("id = :sid")
        bucket_where.append("i.source_id = :sid")
        params["sid"] = source_id
    bucket_params: dict[str, Any] = {**params, "tz": resolved_tz}
    if since is not None:
        bucket_where.append("(i.occurred_at AT TIME ZONE :tz)::date >= :since")
        bucket_params["since"] = since
    if until is not None:
        bucket_where.append("(i.occurred_at AT TIME ZONE :tz)::date <= :until")
        bucket_params["until"] = until

    with connection() as conn:
        src_rows = (
            conn.execute(
                text(
                    "SELECT id, name, type, enabled FROM sources "
                    f"WHERE {' AND '.join(src_where)} ORDER BY id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        # Buckets diarios por fuente: total ingerido vs manejados bajo el criterio. El LEFT JOIN
        # a classifications no duplica filas (UNIQUE(inbox_id)).
        bucket_rows = conn.execute(
            text(
                f"""
                SELECT i.source_id,
                       (i.occurred_at AT TIME ZONE :tz)::date AS day,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE {_HANDLED_SQL[criterion]}) AS handled
                FROM inbox i
                LEFT JOIN classifications c ON c.inbox_id = i.id
                WHERE {" AND ".join(bucket_where)}
                GROUP BY i.source_id, day
                ORDER BY i.source_id, day
                """
            ),
            bucket_params,
        ).all()
        # Frontera del lote (a lo sumo uno por user): los arrays de PG son 1-based, así que
        # `target_ids[frontier]` ES el último ya procesado; frontier=0 (o inbox borrado) deja
        # `frontier_at` NULL → sin marcador.
        lot_row = (
            conn.execute(
                text(
                    """
                    SELECT pl.frontier, cardinality(pl.target_ids) AS total_targets,
                           pl.config, pl.updated_at, i.occurred_at AS frontier_at
                    FROM processing_lots pl
                    LEFT JOIN inbox i ON i.id = pl.target_ids[pl.frontier]
                    WHERE pl.user_id = :uid
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .first()
        )

    # Por fuente: segmentos de días COMPLETOS separados por días con pendientes (parciales o sin
    # nada manejado) — la fusión por gap_days corre POR SEGMENTO, así nunca puentea un pendiente;
    # los días sin mensajes no cortan nada (no hay bucket) y sí se pueden fundir.
    full_by_source: dict[int, list[list[tuple[date, int]]]] = {}
    partial_by_source: dict[int, list[tuple[date, date]]] = {}
    handled_by_source: dict[int, int] = {}
    for sid, day, total, handled in bucket_rows:
        handled_by_source[sid] = handled_by_source.get(sid, 0) + int(handled)
        segments = full_by_source.setdefault(sid, [[]])
        if handled == total:
            segments[-1].append((day, int(handled)))
        else:
            if segments[-1]:
                segments.append([])
            if handled > 0:
                partial_by_source.setdefault(sid, []).append((day, day + timedelta(days=1)))

    lot_cursor: dict[str, Any] | None = None
    lot_source_id: int | None = None
    if lot_row is not None and lot_row["frontier_at"] is not None:
        cur_day = lot_row["frontier_at"].astimezone(ZoneInfo(resolved_tz)).date()
        if (since is None or cur_day >= since) and (until is None or cur_day <= until):
            filters = dict(lot_row["config"] or {}).get("filters") or {}
            lot_source_id = filters.get("source_id")
            lot_cursor = {
                "at": lot_row["updated_at"],
                "day": cur_day,
                "summary": f"lote: {lot_row['frontier']}/{lot_row['total_targets']} mensajes",
            }

    lanes: list[dict[str, Any]] = []
    domain_lo: list[date] = []
    domain_hi: list[date] = []
    for src in src_rows:
        try:
            src_kind = kind_for_type(src["type"]).value
        except KeyError:
            src_kind = "other"  # tipos sin SourceKind registrada (p.ej. seeds viejos)
        if kind is not None and src_kind != kind:
            continue
        ranges = [
            r for seg in full_by_source.get(src["id"], []) for r in merge_day_buckets(seg, gap_days)
        ]
        swept = merge_date_spans(partial_by_source.get(src["id"], []))
        cursor_info = (
            lot_cursor if lot_cursor is not None and lot_source_id in (None, src["id"]) else None
        )
        lanes.append(
            {
                "id": src["id"],
                "label": src["name"],
                "kind": src_kind,
                "enabled": src["enabled"],
                "total": handled_by_source.get(src["id"], 0),
                "first_day": ranges[0]["start"] if ranges else None,
                "last_day": ranges[-1]["end"] if ranges else None,
                "ranges": ranges,
                "swept": swept,
                "cursor": cursor_info,
            }
        )
        # El dominio del eje abarca completos, parciales y marcador (todo es estado del pipeline).
        domain_lo += [r["start"] for r in (ranges[:1] + swept[:1])]
        domain_hi += [r["end"] for r in (ranges[-1:] + swept[-1:])]
        if cursor_info is not None:
            domain_lo.append(cursor_info["day"])
            domain_hi.append(cursor_info["day"])

    return {
        "lanes": lanes,
        # Con ventana pedida el eje ES la ventana (aunque esté vacía); sin ella, los extremos
        # de los datos. Lado pedido a medias: el faltante sale de los datos (o queda None).
        "domain_min": since if since is not None else (min(domain_lo) if domain_lo else None),
        "domain_max": until if until is not None else (max(domain_hi) if domain_hi else None),
        "tz": resolved_tz,
        "gap_days": gap_days,
    }
