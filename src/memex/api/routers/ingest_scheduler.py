"""Ingesta agendada desde /carga: control runtime del daemon `memex-ingest-scheduler` + historial.

Dos superficies (hermanas de las de `/processing` para procesamiento):

1. **Control del daemon** (`GET/PATCH /ingest/scheduler`). GET combina el master toggle de DB
   (`ingest_scheduler_settings`) con, por fuente, su `fetch_schedule` y su ÚLTIMA corrida real
   (`ingestion_runs`). PATCH prende/apaga el master; el daemon relee la DB cada tick. El intervalo
   por fuente se setea por `PATCH /sources/{id}` (`fetch_schedule`), no acá. Apagado por default.

2. **Historial de corridas** (`GET /ingest/runs`). Lista `ingestion_runs` recientes con su ORIGEN
   (`trigger`: manual/daemon/backfill/agent/cli) + stats. `id` (UUID) es la clave del deep-link a
   `/logs?run_id=<id>` para ver las líneas de esa corrida.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from memex.api.auth import current_user_id
from memex.api.schemas import (
    IngestionRunList,
    IngestionRunRow,
    IngestSchedulerPatch,
    IngestSchedulerState,
    IngestScheduleSource,
)
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/ingest", tags=["ingest-scheduler"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.ingest_scheduler")

#: Mismo umbral de "colgado" que processing.py / stats.py (corrida 'running' pasado este tiempo).
_STALE = "interval '30 minutes'"

#: Columnas comunes de una corrida + el flag is_stale calculado (reusado por scheduler y runs).
_RUN_COLUMNS = f"""
    id, source_id, trigger, status, started_at, ended_at, duration_ms,
    posted, inserted, duplicates, errors, filtered, error_class, error_message,
    api_cost_usd,
    (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
"""


def _run_row(r: RowMapping) -> IngestionRunRow:
    """Fila de `ingestion_runs` → modelo. `id` (UUID de la DB) se serializa como string."""
    return IngestionRunRow(
        id=str(r["id"]),
        source_id=int(r["source_id"]),
        trigger=str(r["trigger"]),
        status=str(r["status"]),
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        duration_ms=r["duration_ms"],
        posted=int(r["posted"]),
        inserted=int(r["inserted"]),
        duplicates=int(r["duplicates"]),
        errors=int(r["errors"]),
        filtered=int(r["filtered"]),
        error_class=r["error_class"],
        error_message=r["error_message"],
        api_cost_usd=float(r["api_cost_usd"]) if r["api_cost_usd"] is not None else None,
        is_stale=bool(r["is_stale"]),
    )


@router.get("/scheduler", response_model=IngestSchedulerState)
async def get_ingest_scheduler(user_id: UserID) -> dict[str, Any]:
    """Estado del daemon de ingesta: master toggle + por fuente su schedule y última corrida."""
    with connection() as conn:
        srow = (
            conn.execute(
                text("SELECT daemon_enabled FROM ingest_scheduler_settings WHERE user_id = :uid"),
                {"uid": user_id},
            )
            .mappings()
            .first()
        )
        source_rows = (
            conn.execute(
                text(
                    "SELECT s.id, s.name, s.type, s.enabled, s.config, s.fetch_schedule, "
                    "a.alias AS account_alias, "
                    "COALESCE(s.config->>'account_email', a.metadata->>'email') AS account_email "
                    "FROM sources s LEFT JOIN accounts a ON a.id = s.account_id "
                    "WHERE s.user_id = :uid ORDER BY s.id"
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
        latest_by_source = {
            int(r["source_id"]): _run_row(r)
            for r in conn.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (source_id) {_RUN_COLUMNS}
                    FROM ingestion_runs
                    WHERE user_id = :uid
                    ORDER BY source_id, started_at DESC
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        }

    daemon_enabled = bool(srow["daemon_enabled"]) if srow else False
    sources = [
        IngestScheduleSource(
            source_id=int(s["id"]),
            name=str(s["name"]),
            type=str(s["type"]),
            enabled=bool(s["enabled"]),
            config=dict(s["config"] or {}),
            fetch_schedule=s["fetch_schedule"],
            account_alias=s["account_alias"],
            account_email=s["account_email"],
            latest=latest_by_source.get(int(s["id"])),
        )
        for s in source_rows
    ]
    return {"daemon_enabled": daemon_enabled, "sources": sources}


@router.patch("/scheduler", response_model=IngestSchedulerState)
async def patch_ingest_scheduler(body: IngestSchedulerPatch, user_id: UserID) -> dict[str, Any]:
    """Prende/apaga el master toggle del daemon de ingesta. El daemon lo relee cada tick."""
    fields = body.model_dump(exclude_unset=True)
    if "daemon_enabled" in fields:
        with connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ingest_scheduler_settings (user_id, daemon_enabled)
                    VALUES (:uid, :de)
                    ON CONFLICT (user_id) DO UPDATE SET daemon_enabled = :de, updated_at = NOW()
                    """
                ),
                {"uid": user_id, "de": fields["daemon_enabled"]},
            )
        _log.info(
            "ingest.scheduler.patched", user_id=user_id, daemon_enabled=fields["daemon_enabled"]
        )
    return await get_ingest_scheduler(user_id)


@router.get("/runs", response_model=IngestionRunList)
async def list_ingestion_runs(
    user_id: UserID,
    source_id: Annotated[int | None, Query()] = None,
    trigger: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Corridas de ingesta recientes (todas las fuentes por default), más nuevas primero.

    Filtros opcionales: `source_id` y `trigger` (origen). La UI de /carga poll-ea acá y linkea cada
    corrida a `/logs?run_id=<id>`.
    """
    clauses = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if source_id is not None:
        clauses.append("source_id = :source_id")
        params["source_id"] = source_id
    if trigger:
        clauses.append("trigger = :trigger")
        params["trigger"] = trigger
    where = " AND ".join(clauses)
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT {_RUN_COLUMNS}
                    FROM ingestion_runs
                    WHERE {where}
                    ORDER BY started_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    return {"items": [_run_row(r) for r in rows]}
