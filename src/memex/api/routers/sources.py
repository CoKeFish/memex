import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from memex import sources as source_registry
from memex.api.auth import current_user_id
from memex.api.inprocess_sink import DryRunSink, InProcessSink
from memex.api.schemas import CheckpointBody, FetchResponse, SourceCreate, SourceRow
from memex.core import checkpoint
from memex.core.observability import ingestion_run
from memex.core.sink import MemexSink
from memex.core.source import SourceConfigError
from memex.db import connection
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger

router = APIRouter(prefix="/sources", tags=["sources"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.sources")


def _assert_owns_source(conn: Any, user_id: int, source_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="source not found")


@router.get("", response_model=list[SourceRow])
async def list_sources(user_id: UserID) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, user_id, name, type, enabled, config, created_at
                    FROM sources WHERE user_id = :uid ORDER BY id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@router.post("", response_model=SourceRow, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    try:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        INSERT INTO sources (user_id, name, type, config)
                        VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                        RETURNING id, user_id, name, type, enabled, config, created_at
                        """
                    ),
                    {
                        "uid": user_id,
                        "name": body.name,
                        "type": body.type,
                        "cfg": json.dumps(body.config),
                    },
                )
                .mappings()
                .first()
            )
    except IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="source with that name already exists for this user"
        ) from e
    assert row is not None
    _log.info(
        "sources.created",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
    )
    return dict(row)


@router.post("/ensure", response_model=SourceRow)
async def ensure_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    """Get-or-create idempotente por (user_id, name).

    Si la fuente ya existe para este usuario con ese nombre, la devuelve sin
    tocarla. Si no existe, la crea con `type` y `config` provistos.
    """
    with connection() as conn:
        existing = (
            conn.execute(
                text(
                    """
                    SELECT id, user_id, name, type, enabled, config, created_at
                    FROM sources WHERE user_id = :uid AND name = :name
                    """
                ),
                {"uid": user_id, "name": body.name},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            _log.info(
                "sources.ensured",
                user_id=user_id,
                source_id=existing["id"],
                name=existing["name"],
                source_type=existing["type"],
                action="existed",
            )
            return dict(existing)
        row = (
            conn.execute(
                text(
                    """
                    INSERT INTO sources (user_id, name, type, config)
                    VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                    RETURNING id, user_id, name, type, enabled, config, created_at
                    """
                ),
                {
                    "uid": user_id,
                    "name": body.name,
                    "type": body.type,
                    "cfg": json.dumps(body.config),
                },
            )
            .mappings()
            .first()
        )
    assert row is not None
    _log.info(
        "sources.ensured",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
        action="created",
    )
    return dict(row)


@router.get("/{source_id}/checkpoint")
async def get_checkpoint(source_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        cur = checkpoint.get_cursor(conn, source_id)
    return {"cursor": cur}


@router.put("/{source_id}/checkpoint")
async def put_checkpoint(source_id: int, body: CheckpointBody, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        checkpoint.save_cursor(conn, source_id, body.cursor)
    _log.info(
        "sources.checkpoint.updated",
        user_id=user_id,
        source_id=source_id,
    )
    return {"cursor": body.cursor}


def _stats_response(stats: RunStats, *, dry_run: bool) -> dict[str, Any]:
    return {
        "posted": stats.posted,
        "inserted": stats.inserted,
        "duplicates": stats.duplicates,
        "errors": stats.errors,
        "filtered": stats.filtered,
        "dry_run": dry_run,
        "ms_elapsed": stats.ms_elapsed,
    }


@router.post("/{source_id}/fetch", response_model=FetchResponse)
async def fetch_source(
    source_id: int,
    user_id: UserID,
    dry_run: Annotated[bool, Query()] = False,
    mode: Annotated[str, Query()] = "incremental",
    since: Annotated[str | None, Query(description="range: YYYY-MM-DD inclusive")] = None,
    until: Annotated[str | None, Query(description="range: YYYY-MM-DD exclusiva")] = None,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="last/range: tope de mensajes")
    ] = None,
) -> dict[str, Any]:
    """Dispara una corrida de ingesta a demanda DENTRO del proceso API (sin CLI).

    Corre `run_ingestor` en un threadpool (es sync + I/O bloqueante). En `dry_run` cuenta
    nuevos/duplicados/filtrados sin escribir. Modos:
      - `incremental`: trae lo nuevo desde el checkpoint y lo AVANZA.
      - `range`: ventana `since`..`until` (backfill). NO toca el checkpoint.
      - `last`: los `limit` más recientes (backfill). NO toca el checkpoint.
    """
    if mode not in ("incremental", "range", "last"):
        raise HTTPException(status_code=422, detail=f"mode {mode!r} inválido")
    if mode == "range" and not since:
        raise HTTPException(status_code=422, detail="mode 'range' requiere el parámetro 'since'")

    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        row = (
            conn.execute(
                text("SELECT type, config FROM sources WHERE id = :sid"),
                {"sid": source_id},
            )
            .mappings()
            .first()
        )
    assert row is not None
    source_type = str(row["type"])
    try:
        factory = source_registry.resolve(source_type)
    except KeyError as e:
        raise HTTPException(
            status_code=422,
            detail=f"source type {source_type!r} no se puede traer desde el server (sin ingestor)",
        ) from e

    # Override transitorio de la ventana de fetch (no se persiste en sources.config).
    cfg = dict(row["config"] or {})
    if mode != "incremental":
        cfg["fetch_mode"] = mode
        if since:
            cfg["fetch_since"] = since
        if until:
            cfg["fetch_until"] = until
        if limit is not None:
            cfg["fetch_limit"] = limit
    try:
        source = factory(cfg)
    except SourceConfigError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    _log.info(
        "fetch.requested",
        user_id=user_id,
        source_id=source_id,
        dry_run=dry_run,
        mode=mode,
        since=since,
        until=until,
        limit=limit,
    )

    if dry_run:
        dry_sink: MemexSink = DryRunSink(user_id)
        stats = await run_in_threadpool(run_ingestor, source, source_id, dry_sink, chunk_sleep_ms=0)
        return _stats_response(stats, dry_run=True)

    # range/last son backfills: insertan pero no avanzan el cursor incremental.
    sink: MemexSink = InProcessSink(user_id, persist_checkpoint=(mode == "incremental"))
    with ingestion_run(user_id=user_id, source_id=source_id, trigger="dashboard") as run:
        try:
            stats = await run_in_threadpool(run_ingestor, source, source_id, sink, chunk_sleep_ms=0)
            run.finalize(stats)
        except Exception as e:
            run.fail(e)
            raise HTTPException(status_code=502, detail=f"fetch falló: {e}") from e
    return _stats_response(stats, dry_run=False)
