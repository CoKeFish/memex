"""Feed de logs consultable sobre la tabla `log_events` (el sink de structlog, migración 0020).

Dos endpoints para la vista /logs:
- `GET /logs` — filas crudas con filtros incluir/excluir multi-valor (level/event/logger),
  escalares de correlación (request_id/run_id/source_id/inbox_id), búsqueda substring (`q`)
  y paginación offset (necesita total para el pager "X-Y de N").
- `GET /logs/stats` — agregaciones server-side (GROUP BY) para el panel de métricas: totales y tasa
  de error, cortes por nivel/evento/logger, histograma temporal (granularidad según el rango) y
  percentiles de latencia (sobre los eventos que llevan `fields->>'duration_ms'`).

A diferencia de /metricas (que agrega `llm_calls`), acá la fuente es el feed completo de líneas de
log: se incluyen las filas pre-auth / de infraestructura (`user_id IS NULL`) para un debug íntegro.
Los helpers `_resolve_tz` / `FilterMode` / `_multi_filter` siguen el patrón de `metrics.py`
(copiados inline a propósito: ese router está funcionando y no se toca).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import LogEventList, LogStats
from memex.core.log_sink import sink_health
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/logs", tags=["logs"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.logs")

#: TZ por defecto del bucket del histograma (cuando el cliente no manda `tz`). El front pasa su TZ
#: activa para que los buckets del eje coincidan con su reloj de pared (patrón de metrics.py).
_BUCKET_TZ = "America/Bogota"

#: Niveles que cuentan como error (para `errors` y el histograma). Centralizado para no repetir.
_ERROR_LEVELS = "('error', 'critical')"


def _resolve_tz(tz: str | None) -> str:
    """Valida/resuelve la TZ del bucket. None → `_BUCKET_TZ`; nombre IANA inválido → 422.

    El valor va al SQL como bind param (`:tz` en `AT TIME ZONE`), no es injection; pero un nombre
    inválido reventaría en Postgres, así que se valida acá contra el catálogo IANA (patrón de
    metrics.py).
    """
    if tz is None:
        return _BUCKET_TZ
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"timezone inválida: {tz}") from exc
    return tz


FilterMode = Literal["include", "exclude"]


def _multi_filter(
    col: str,
    name: str,
    values: list[Any] | None,
    mode: str,
    clauses: list[str],
    params: dict[str, Any],
) -> None:
    """Filtro incluir/excluir multi-valor sobre `col` (= ANY) — vacío/None = sin filtro.

    Patrón de metrics.py: `NOT (...)` cuando `mode == "exclude"`.
    """
    if not values:
        return
    params[name] = values
    expr = f"{col} = ANY(:{name})"
    clauses.append(f"NOT ({expr})" if mode == "exclude" else expr)


def _window(since: datetime | None, until: datetime | None, params: dict[str, Any]) -> list[str]:
    """Cláusulas base de ventana sobre `log_events le`. Agrega :since/:until a `params`.

    La cláusula base incluye las líneas pre-auth / de infraestructura (`user_id IS NULL`) para
    que el feed de debug sea completo, no solo lo atribuido al usuario actual.
    """
    clauses = ["(le.user_id = :uid OR le.user_id IS NULL)"]
    if since is not None:
        clauses.append("le.ts >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("le.ts < :until")
        params["until"] = until
    return clauses


def _build_where(
    user_id: int,
    since: datetime | None,
    until: datetime | None,
    level: list[str] | None,
    level_mode: str,
    event: list[str] | None,
    event_mode: str,
    logger: list[str] | None,
    logger_mode: str,
    request_id: str | None,
    run_id: str | None,
    source_id: int | None,
    inbox_id: int | None,
    q: str | None,
) -> tuple[str, dict[str, Any]]:
    """Arma el WHERE común a /logs y /logs/stats. Devuelve (where_sql, params).

    Combina la ventana base con los filtros incluir/excluir multi-valor (level/event/logger), los
    escalares de correlación y la búsqueda substring `q` (event + fields::text + exception).
    """
    params: dict[str, Any] = {"uid": user_id}
    clauses = _window(since, until, params)

    _multi_filter("le.level", "level", level, level_mode, clauses, params)
    _multi_filter("le.event", "event", event, event_mode, clauses, params)
    _multi_filter("le.logger", "logger", logger, logger_mode, clauses, params)

    if request_id is not None:
        clauses.append("le.request_id = :request_id")
        params["request_id"] = request_id
    if run_id is not None:
        clauses.append("le.run_id = :run_id")
        params["run_id"] = run_id
    if source_id is not None:
        clauses.append("le.source_id = :source_id")
        params["source_id"] = source_id
    if inbox_id is not None:
        clauses.append("le.inbox_id = :inbox_id")
        params["inbox_id"] = inbox_id

    if q:
        params["q"] = f"%{q}%"
        clauses.append(
            "(le.event ILIKE :q OR le.fields::text ILIKE :q OR COALESCE(le.exception, '') ILIKE :q)"
        )

    return " AND ".join(clauses), params


#: Columnas permitidas para ordenar (nunca interpolar input del usuario como columna). Solo `ts` por
#: ahora; el desempate por `id` mantiene el orden estable entre páginas.
_SORT_COLS = {"ts": "le.ts"}


@router.get("", response_model=LogEventList)
async def list_logs(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    tz: str | None = None,
    level: Annotated[list[str] | None, Query()] = None,
    level_mode: FilterMode = "include",
    event: Annotated[list[str] | None, Query()] = None,
    event_mode: FilterMode = "include",
    logger: Annotated[list[str] | None, Query()] = None,
    logger_mode: FilterMode = "include",
    request_id: str | None = None,
    run_id: str | None = None,
    source_id: int | None = None,
    inbox_id: int | None = None,
    q: str | None = None,
    sort: Literal["ts"] = "ts",
    dir: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Feed de `log_events`: filas crudas filtrables/paginadas (debug).

    Filtros incluir/excluir multi-valor por nivel/evento/logger; escalares de correlación
    (request_id/run_id/source_id/inbox_id); `q` busca substring en event + fields + exception.
    `sort` sale de una whitelist fija (solo `ts`).
    """
    _resolve_tz(tz)  # valida (422 si inválida); la lista no bucketea → no se usa el valor más
    where, params = _build_where(
        user_id,
        since,
        until,
        level,
        level_mode,
        event,
        event_mode,
        logger,
        logger_mode,
        request_id,
        run_id,
        source_id,
        inbox_id,
        q,
    )
    params["limit"] = limit
    params["offset"] = offset

    col = _SORT_COLS[sort]
    direction = "ASC" if dir == "asc" else "DESC"

    sql = f"""
        SELECT le.id, le.ts, le.level, le.event, le.logger, le.user_id, le.request_id,
               le.run_id, le.source_id, le.inbox_id, le.exception, le.fields,
               COUNT(*) OVER() AS total
        FROM log_events le
        WHERE {where}
        ORDER BY {col} {direction}, le.id {direction}
        LIMIT :limit OFFSET :offset
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    total = int(rows[0]["total"]) if rows else 0
    # `fields` ya viene como dict de psycopg (columna JSONB); el resto son escalares directos.
    items = [
        {
            "id": r["id"],
            "ts": r["ts"],
            "level": r["level"],
            "event": r["event"],
            "logger": r["logger"],
            "user_id": r["user_id"],
            "request_id": r["request_id"],
            "run_id": r["run_id"],
            "source_id": r["source_id"],
            "inbox_id": r["inbox_id"],
            "exception": r["exception"],
            "fields": r["fields"],
        }
        for r in rows
    ]
    _log.info("logs.query", user_id=user_id, total=total, returned=len(items))
    return {"items": items, "total": total}


def _histogram_granularity(since: datetime | None, until: datetime | None) -> str:
    """Elige la granularidad del histograma desde el span del rango.

    Con `since` y `until`: <=2h → minuto, <=4d → hora, si no → día. Si falta alguno (rango abierto)
    se usa 'hour' como término medio razonable para un feed de debug.
    """
    if since is None or until is None:
        return "hour"
    span = until - since
    if span.total_seconds() <= 2 * 3600:
        return "minute"
    if span.total_seconds() <= 4 * 86400:
        return "hour"
    return "day"


@router.get("/stats", response_model=LogStats)
async def logs_stats(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    tz: str | None = None,
    level: Annotated[list[str] | None, Query()] = None,
    level_mode: FilterMode = "include",
    event: Annotated[list[str] | None, Query()] = None,
    event_mode: FilterMode = "include",
    logger: Annotated[list[str] | None, Query()] = None,
    logger_mode: FilterMode = "include",
    request_id: str | None = None,
    run_id: str | None = None,
    source_id: int | None = None,
    inbox_id: int | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Agregaciones de `log_events` del rango filtrado para el panel de métricas de /logs.

    Mismos filtros que `/logs` (menos sort/dir/limit/offset). Devuelve totales y tasa de error,
    cortes por nivel/evento/logger (top 20 los dos últimos), histograma temporal (granularidad según
    el rango) y percentiles de latencia sobre `fields->>'duration_ms'`. `sink_dropped` viene del
    health del sink (eventos descartados por overflow de la cola, no silent cap).
    """
    resolved_tz = _resolve_tz(tz)
    where, params = _build_where(
        user_id,
        since,
        until,
        level,
        level_mode,
        event,
        event_mode,
        logger,
        logger_mode,
        request_id,
        run_id,
        source_id,
        inbox_id,
        q,
    )

    gran = _histogram_granularity(since, until)
    hist_params = dict(params)
    hist_params["gran"] = gran
    hist_params["tz"] = resolved_tz

    with connection() as conn:
        totals = (
            conn.execute(
                text(f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE le.level IN {_ERROR_LEVELS}) AS errors
                FROM log_events le
                WHERE {where}
            """),
                params,
            )
            .mappings()
            .one()
        )

        by_level = (
            conn.execute(
                text(f"""
                SELECT le.level, COUNT(*) AS count
                FROM log_events le
                WHERE {where}
                GROUP BY le.level
                ORDER BY count DESC
            """),
                params,
            )
            .mappings()
            .all()
        )

        by_event = (
            conn.execute(
                text(f"""
                SELECT le.event, COUNT(*) AS count
                FROM log_events le
                WHERE {where}
                GROUP BY le.event
                ORDER BY count DESC
                LIMIT 20
            """),
                params,
            )
            .mappings()
            .all()
        )

        by_logger = (
            conn.execute(
                text(f"""
                SELECT le.logger, COUNT(*) AS count
                FROM log_events le
                WHERE {where} AND le.logger IS NOT NULL
                GROUP BY le.logger
                ORDER BY count DESC
                LIMIT 20
            """),
                params,
            )
            .mappings()
            .all()
        )

        histogram = (
            conn.execute(
                text(f"""
                SELECT date_trunc(:gran, (le.ts AT TIME ZONE :tz)) AS bucket,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE le.level IN {_ERROR_LEVELS}) AS errors
                FROM log_events le
                WHERE {where}
                GROUP BY bucket
                ORDER BY bucket
            """),
                hist_params,
            )
            .mappings()
            .all()
        )

        # Percentiles solo sobre las filas que llevan `duration_ms` en `fields`; sin ninguna, los
        # tres salen NULL y el modelo los deja en None.
        latency = (
            conn.execute(
                text(f"""
                SELECT
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY (le.fields->>'duration_ms')::numeric
                    ) FILTER (WHERE le.fields ? 'duration_ms') AS p50,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY (le.fields->>'duration_ms')::numeric
                    ) FILTER (WHERE le.fields ? 'duration_ms') AS p95,
                    percentile_cont(0.99) WITHIN GROUP (
                        ORDER BY (le.fields->>'duration_ms')::numeric
                    ) FILTER (WHERE le.fields ? 'duration_ms') AS p99
                FROM log_events le
                WHERE {where}
            """),
                params,
            )
            .mappings()
            .one()
        )

    total = int(totals["total"])
    errors = int(totals["errors"])

    def _pct(value: Any) -> float | None:
        return float(value) if value is not None else None

    _log.info("logs.stats", user_id=user_id, total=total, errors=errors)
    return {
        "total": total,
        "errors": errors,
        "error_rate": (errors / total) if total else 0.0,
        "by_level": [{"level": r["level"], "count": int(r["count"])} for r in by_level],
        "by_event": [{"event": r["event"], "count": int(r["count"])} for r in by_event],
        "by_logger": [{"logger": r["logger"], "count": int(r["count"])} for r in by_logger],
        "histogram": [
            {"bucket": r["bucket"], "total": int(r["total"]), "errors": int(r["errors"])}
            for r in histogram
        ],
        "latency": {
            "p50": _pct(latency["p50"]),
            "p95": _pct(latency["p95"]),
            "p99": _pct(latency["p99"]),
        },
        "sink_dropped": int(sink_health()["dropped"]),
    }
