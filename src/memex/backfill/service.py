"""Backfill segmentado: estado persistido (`backfill_jobs`) + orquestación del avance por ventanas.

Vive en `backfill/` (peer de `api`, NO en `ingestors/*`, que tiene prohibido importar `memex.db` /
`memex.api`). El avance reusa `memex.api.fetch_runner.run_fetch_window` (mode=range): inserta sin
mover el cursor incremental. La frontera se persiste SOLO si la ventana corrió sin excepción, así
una corrida interrumpida se re-ejecuta sobre la misma ventana (idempotente por dedup).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import Connection, text

from memex.api.fetch_runner import run_fetch_window
from memex.api.schemas import BackfillState, BackfillWindowResult
from memex.backfill import windows
from memex.db import connection
from memex.logging import get_logger

_log = get_logger("memex.backfill.service")

_NO_JOB = "no hay backfill configurado para esta fuente"


@dataclass
class BackfillJob:
    """Fila de `backfill_jobs` materializada. `range_end` es EXCLUSIVO (como en la DB)."""

    source_id: int
    range_start: date
    range_end: date
    frontier: date
    window_unit: windows.WindowUnit
    window_count: int
    per_window_limit: int
    status: str
    history: list[dict[str, Any]]


_SELECT = """
    SELECT source_id, range_start, range_end, frontier, window_unit, window_count,
           per_window_limit, status, history
    FROM backfill_jobs
    WHERE source_id = :sid AND user_id = :uid
"""


def _row_to_job(row: Any) -> BackfillJob:
    return BackfillJob(
        source_id=int(row["source_id"]),
        range_start=row["range_start"],
        range_end=row["range_end"],
        frontier=row["frontier"],
        window_unit=cast("windows.WindowUnit", str(row["window_unit"])),
        window_count=int(row["window_count"]),
        per_window_limit=int(row["per_window_limit"]),
        status=str(row["status"]),
        history=list(row["history"] or []),
    )


def get_job(conn: Connection, user_id: int, source_id: int) -> BackfillJob | None:
    row = conn.execute(text(_SELECT), {"sid": source_id, "uid": user_id}).mappings().first()
    return _row_to_job(row) if row is not None else None


def upsert_job(
    conn: Connection,
    user_id: int,
    source_id: int,
    *,
    range_start: date,
    range_end_inclusive: date,
    window_unit: windows.WindowUnit,
    window_count: int,
    per_window_limit: int,
) -> BackfillJob:
    """Crea o reconfigura el backfill: frontera = range_start, history vacío, estado activo."""
    range_end = range_end_inclusive + timedelta(days=1)  # inclusivo (UI) → exclusivo (DB)
    conn.execute(
        text(
            """
            INSERT INTO backfill_jobs
              (user_id, source_id, range_start, range_end, frontier,
               window_unit, window_count, per_window_limit, status, history)
            VALUES
              (:uid, :sid, :rs, :re, :rs, :u, :c, :lim, 'active', '[]'::jsonb)
            ON CONFLICT (source_id) DO UPDATE SET
              range_start      = EXCLUDED.range_start,
              range_end        = EXCLUDED.range_end,
              frontier         = EXCLUDED.range_start,
              window_unit      = EXCLUDED.window_unit,
              window_count     = EXCLUDED.window_count,
              per_window_limit = EXCLUDED.per_window_limit,
              status           = 'active',
              history          = '[]'::jsonb,
              updated_at       = NOW()
            """
        ),
        {
            "uid": user_id,
            "sid": source_id,
            "rs": range_start,
            "re": range_end,
            "u": window_unit,
            "c": window_count,
            "lim": per_window_limit,
        },
    )
    job = get_job(conn, user_id, source_id)
    assert job is not None
    return job


def delete_job(conn: Connection, user_id: int, source_id: int) -> bool:
    res = conn.execute(
        text("DELETE FROM backfill_jobs WHERE source_id = :sid AND user_id = :uid"),
        {"sid": source_id, "uid": user_id},
    )
    return bool(res.rowcount)


def to_state(job: BackfillJob) -> BackfillState:
    """Estado para la UI: `range_end` vuelve INCLUSIVO y el % sale de la frontera."""
    return BackfillState(
        source_id=job.source_id,
        range_start=job.range_start,
        range_end=job.range_end - timedelta(days=1),
        frontier=job.frontier,
        window_unit=job.window_unit,
        window_count=job.window_count,
        per_window_limit=job.per_window_limit,
        status=job.status,
        progress_pct=windows.progress_pct(job.range_start, job.range_end, job.frontier),
        history=[BackfillWindowResult.model_validate(h) for h in job.history],
    )


def _load_source(conn: Connection, source_id: int) -> tuple[str, dict[str, Any], int | None]:
    row = (
        conn.execute(
            text("SELECT type, config, account_id FROM sources WHERE id = :sid"),
            {"sid": source_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return str(row["type"]), dict(row["config"] or {}), row["account_id"]


def _persist_advance(
    conn: Connection,
    *,
    user_id: int,
    source_id: int,
    range_end: date,
    new_frontier: date,
    unit: windows.WindowUnit,
    count: int,
    result: BackfillWindowResult,
) -> BackfillJob:
    """Mueve la frontera, appendea la ventana al history y guarda el tamaño como nuevo default."""
    row = (
        conn.execute(
            text(
                "SELECT history FROM backfill_jobs "
                "WHERE source_id = :sid AND user_id = :uid FOR UPDATE"
            ),
            {"sid": source_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail=_NO_JOB)
    history = list(row["history"] or [])
    history.append(result.model_dump(mode="json"))
    status = "done" if windows.is_done(new_frontier, range_end) else "active"
    conn.execute(
        text(
            """
            UPDATE backfill_jobs SET
              frontier     = :f,
              window_unit  = :u,
              window_count = :c,
              status       = :s,
              history      = CAST(:h AS JSONB),
              updated_at   = NOW()
            WHERE source_id = :sid AND user_id = :uid
            """
        ),
        {
            "f": new_frontier,
            "u": unit,
            "c": count,
            "s": status,
            "h": json.dumps(history),
            "sid": source_id,
            "uid": user_id,
        },
    )
    job = get_job(conn, user_id, source_id)
    assert job is not None
    return job


async def _advance(
    user_id: int,
    source_id: int,
    *,
    dry_run: bool,
    unit: windows.WindowUnit | None,
    count: int | None,
    to_end_of_range: bool,
) -> tuple[BackfillWindowResult | None, BackfillJob]:
    with connection() as conn:
        job = get_job(conn, user_id, source_id)
        if job is None:
            raise HTTPException(status_code=404, detail=_NO_JOB)
        source_type, cfg, account_id = _load_source(conn, source_id)

    if windows.is_done(job.frontier, job.range_end):
        return None, job  # no-op: ya estaba completo

    if to_end_of_range:
        start, end = job.frontier, job.range_end
        store_unit, store_count = job.window_unit, job.window_count
    else:
        store_unit = unit or job.window_unit
        store_count = count or job.window_count
        start, end = windows.next_window(job.frontier, job.range_end, store_unit, store_count)

    # Si esto levanta (502/422), la frontera NO se persiste → la próxima corrida repite la ventana.
    stats = await run_fetch_window(
        user_id=user_id,
        source_id=source_id,
        source_type=source_type,
        cfg=cfg,
        account_id=account_id,
        mode="range",
        since=start.isoformat(),
        until=end.isoformat(),
        limit=job.per_window_limit,
        dry_run=dry_run,
        trigger="backfill",
    )
    result = BackfillWindowResult(
        start=start,
        end=end,
        posted=stats.posted,
        inserted=stats.inserted,
        duplicates=stats.duplicates,
        errors=stats.errors,
        filtered=stats.filtered,
        cap_hit=stats.posted >= job.per_window_limit,
        ms_elapsed=stats.ms_elapsed,
        at=datetime.now(UTC),
    )
    if dry_run:
        return result, job  # preview: no mueve la frontera ni toca el history

    with connection() as conn:
        updated = _persist_advance(
            conn,
            user_id=user_id,
            source_id=source_id,
            range_end=job.range_end,
            new_frontier=end,
            unit=store_unit,
            count=store_count,
            result=result,
        )
    _log.info(
        "backfill.window.done",
        user_id=user_id,
        source_id=source_id,
        start=start.isoformat(),
        end=end.isoformat(),
        inserted=result.inserted,
        duplicates=result.duplicates,
        cap_hit=result.cap_hit,
        status=updated.status,
    )
    return result, updated


async def advance_one(
    user_id: int,
    source_id: int,
    *,
    dry_run: bool,
    unit_override: windows.WindowUnit | None = None,
    count_override: int | None = None,
) -> tuple[BackfillWindowResult | None, BackfillJob]:
    """Avanza UNA ventana del tamaño guardado (o el override, que queda como nuevo default)."""
    return await _advance(
        user_id,
        source_id,
        dry_run=dry_run,
        unit=unit_override,
        count=count_override,
        to_end_of_range=False,
    )


async def advance_rest(
    user_id: int, source_id: int, *, dry_run: bool
) -> tuple[BackfillWindowResult | None, BackfillJob]:
    """Trae todo lo que queda `[frontier, range_end)` en una sola ventana."""
    return await _advance(
        user_id, source_id, dry_run=dry_run, unit=None, count=None, to_end_of_range=True
    )
