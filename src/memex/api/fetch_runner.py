"""Runner compartido del fetch a demanda in-process.

Cuerpo extraído de `POST /sources/{id}/fetch` (`routers/sources.py`) para que ese endpoint y el
backfill segmentado (`routers/backfill.py`) corran una ventana de ingesta SIN duplicar el camino
resolve → sink → runner → ingestion_run.

`mode`:
  - `incremental`: trae lo nuevo desde el checkpoint y lo AVANZA.
  - `range`: ventana `since`..`until` (backfill). NO toca el checkpoint.
  - `last`: los `limit` más recientes (backfill). NO toca el checkpoint.

range/last insertan pero no avanzan el cursor incremental (`persist_checkpoint` solo en
`incremental`); el dedup `UNIQUE(source_id, external_id)` hace idempotente re-correr una ventana.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from memex import sources as source_registry
from memex.api.inprocess_sink import DryRunSink, InProcessSink
from memex.core.observability import ingestion_run
from memex.core.sink import MemexSink
from memex.core.source import SourceConfigError
from memex.db import connection
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger
from memex.sources.resolver import build_resolved_env

_log = get_logger("memex.api.fetch_runner")


async def run_fetch_window(
    *,
    user_id: int,
    source_id: int,
    source_type: str,
    cfg: dict[str, Any],
    account_id: int | None,
    mode: str,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    dry_run: bool,
    trigger: str,
) -> RunStats:
    """Corre UNA ventana de ingesta in-process y devuelve sus stats.

    Inyecta el override transitorio de la ventana en `cfg` (no se persiste en `sources.config`),
    resuelve los secretos del vault (`build_resolved_env`), instancia la `Source` y la corre en un
    threadpool (es sync + I/O bloqueante). Levanta `HTTPException` 422 (tipo sin ingestor / config
    inválida) o 502 (la corrida falló), igual que el endpoint de fetch.
    """
    cfg = dict(cfg)
    if mode != "incremental":
        cfg["fetch_mode"] = mode
        if since:
            cfg["fetch_since"] = since
        if until:
            cfg["fetch_until"] = until
        if limit is not None:
            cfg["fetch_limit"] = limit

    # Inyecta los secretos del vault de la cuenta (si hay) bajo el nombre de su env var. Usa la
    # master key del servidor → funciona sin sesión. Fallback a os.environ si no hay.
    with connection() as conn:
        resolved_env = build_resolved_env(
            conn,
            user_id=user_id,
            source_type=source_type,
            cfg=cfg,
            account_id=account_id,
        )

    try:
        factory = source_registry.resolve(source_type)
    except KeyError as e:
        raise HTTPException(
            status_code=422,
            detail=f"source type {source_type!r} no se puede traer desde el server (sin ingestor)",
        ) from e
    try:
        source = factory(cfg, env=resolved_env)
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
        trigger=trigger,
    )

    if dry_run:
        dry_sink: MemexSink = DryRunSink(user_id)
        return await run_in_threadpool(run_ingestor, source, source_id, dry_sink, chunk_sleep_ms=0)

    # range/last son backfills: insertan pero no avanzan el cursor incremental.
    sink: MemexSink = InProcessSink(user_id, persist_checkpoint=(mode == "incremental"))
    with ingestion_run(user_id=user_id, source_id=source_id, trigger=trigger) as run:
        try:
            stats = await run_in_threadpool(run_ingestor, source, source_id, sink, chunk_sleep_ms=0)
            run.finalize(stats)
        except Exception as e:
            run.fail(e)
            raise HTTPException(status_code=502, detail=f"fetch falló: {e}") from e
    return stats
