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

from contextlib import suppress
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Connection, text
from starlette.concurrency import run_in_threadpool

from memex import sources as source_registry
from memex.api.inprocess_sink import DryRunSink, InProcessSink
from memex.core.observability import ingestion_run, record_apify_runs
from memex.core.sink import MemexSink
from memex.core.source import ActorRunReporting, Source, SourceConfigError
from memex.db import connection, get_engine
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger
from memex.sources.resolver import build_resolved_env

_log = get_logger("memex.api.fetch_runner")

# Clave del advisory lock por-fuente. Incluye current_database() para que NO colisione entre las
# DB efímeras del mismo cluster (xdist en tests); en prod (una sola DB) es estable por source.
_FETCH_LOCK_KEY = "hashtext('ingest_fetch:' || current_database() || ':' || (:sid)::text)"


def _persist_actor_reports(
    source: Source[Any],
    *,
    user_id: int,
    source_id: int,
    ingestion_run_id: str | None,
) -> float | None:
    """Drena y persiste los reports de runs de actor (costo Apify) si la source los expone.

    Se llama en un `finally` (sync, dentro del threadpool): los actores ya corrieron y
    COBRARON aunque el sink o la corrida hayan fallado después — el gasto se persiste
    siempre. Devuelve el costo agregado (float) para la respuesta del fetch, o None.
    Nunca lanza: perder la trazabilidad no debe tumbar una corrida que ya funcionó.
    """
    if not isinstance(source, ActorRunReporting):
        return None
    reports = source.pop_run_reports()
    if not reports:
        return None
    try:
        total = record_apify_runs(
            user_id=user_id,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            reports=reports,
        )
    except Exception as e:
        _log.error(
            "fetch.apify_runs.persist_failed",
            source_id=source_id,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        return None
    return float(total) if total is not None else None


def record_swept_range(
    *,
    user_id: int,
    source_id: int,
    since: str | None,
    until: str | None,
    posted: int,
    limit: int | None,
) -> None:
    """Registra el rango de fechas BARRIDO por una corrida `range` exitosa (overlay del timeline).

    Solo ventanas CERRADAS de fechas puras (`since` inclusivo + `until` exclusivo) y COMPLETAS:
    si `posted >= limit` la ventana pudo quedar truncada por el cap → no se reclama (under-claim
    deliberado; los rangos abiertos o con timestamps tampoco se reclaman). Append-only: el
    solape/dedup lo funde el lector (GET /inbox/coverage). Nunca lanza — perder la marca no debe
    tumbar una corrida que ya funcionó.
    """
    if not since or not until:
        return
    if limit is not None and posted >= limit:
        return
    try:
        start, end = date.fromisoformat(since), date.fromisoformat(until)
    except ValueError:
        return
    if end <= start:
        return
    try:
        with connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO ingest_swept_ranges (user_id, source_id, range_start, range_end)"
                    " VALUES (:uid, :sid, :rs, :re)"
                ),
                {"uid": user_id, "sid": source_id, "rs": start, "re": end},
            )
    except Exception as e:
        _log.error(
            "fetch.swept_range.persist_failed",
            source_id=source_id,
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )


def _acquire_fetch_lock(source_id: int) -> Connection | None:
    """Lock de SESIÓN por-fuente para serializar el fetch incremental.

    Devuelve la conexión que retiene el lock (el caller la libera + cierra en su `finally`) o
    ``None`` si ya está tomado por otra corrida. Es de sesión (no por-tx) porque una corrida
    abarca muchas transacciones cortas del runner; se commitea de inmediato para no dejar la
    conexión idle-in-transaction mientras lo retiene.
    """
    conn = get_engine().connect()
    try:
        got = conn.execute(
            text(f"SELECT pg_try_advisory_lock({_FETCH_LOCK_KEY})"),
            {"sid": source_id},
        ).scalar()
        conn.commit()
    except Exception:
        conn.close()
        raise
    if not got:
        conn.close()
        return None
    return conn


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

    # Serializa el fetch INCREMENTAL del mismo source (daemon vs fetch manual vs CLI): dos a la vez
    # leerían el mismo cursor y harían doble fetch (y doble corrida de actor pago en redes). El lock
    # se retiene toda la corrida; range/last (backfill) y dry-run no tocan el cursor → sin lock.
    lock_conn: Connection | None = None
    if mode == "incremental" and not dry_run:
        lock_conn = _acquire_fetch_lock(source_id)
        if lock_conn is None:
            _log.warning("fetch.skipped_concurrent", source_id=source_id, trigger=trigger)
            raise HTTPException(
                status_code=409,
                detail="ya hay una corrida incremental en curso para esta fuente",
            )
    try:
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
                detail=(
                    f"source type {source_type!r} no se puede traer desde el server (sin ingestor)"
                ),
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
            try:
                stats = await run_in_threadpool(
                    run_ingestor, source, source_id, dry_sink, chunk_sleep_ms=0
                )
            finally:
                # El dry-run NO escribe inbox, pero los actores de Apify corren y COBRAN
                # igual: la trazabilidad del gasto se persiste siempre (sin ingestion_run).
                api_cost = await run_in_threadpool(
                    _persist_actor_reports,
                    source,
                    user_id=user_id,
                    source_id=source_id,
                    ingestion_run_id=None,
                )
            stats.api_cost_usd = api_cost
            return stats

        # range/last son backfills: insertan pero no avanzan el cursor incremental.
        sink: MemexSink = InProcessSink(user_id, persist_checkpoint=(mode == "incremental"))
        with ingestion_run(user_id=user_id, source_id=source_id, trigger=trigger) as run:
            try:
                stats = await run_in_threadpool(
                    run_ingestor, source, source_id, sink, chunk_sleep_ms=0
                )
                run.finalize(stats)
            except Exception as e:
                run.fail(e)
                raise HTTPException(status_code=502, detail=f"fetch falló: {e}") from e
            finally:
                # SIEMPRE (éxito o fallo del sink): los runs de actor ya gastaron. El writer
                # también deja el agregado en ingestion_runs.api_cost_usd de esta corrida.
                api_cost = await run_in_threadpool(
                    _persist_actor_reports,
                    source,
                    user_id=user_id,
                    source_id=source_id,
                    ingestion_run_id=run.id,
                )
            stats.api_cost_usd = api_cost
        # La corrida terminó sin excepción: si fue una ventana de rango cerrada, queda
        # reclamada como BARRIDA (timeline de cobertura) aunque no haya traído nada.
        if mode == "range":
            record_swept_range(
                user_id=user_id,
                source_id=source_id,
                since=since,
                until=until,
                posted=stats.posted,
                limit=limit,
            )
        return stats
    finally:
        if lock_conn is not None:
            with suppress(Exception):
                lock_conn.execute(
                    text(f"SELECT pg_advisory_unlock({_FETCH_LOCK_KEY})"),
                    {"sid": source_id},
                )
            lock_conn.close()
