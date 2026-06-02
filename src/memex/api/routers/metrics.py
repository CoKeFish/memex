"""Métricas de costo LLM sobre la tabla `llm_calls`.

Dos endpoints para la vista /metricas:
- `GET /metrics/llm/rollup` — agregaciones server-side (GROUP BY): KPIs, costo por fuente, por
  módulo (de `purpose`), por modelo, matriz fuente x módulo y serie diaria. A diferencia de
  `/finance/expenses` (filas crudas + agregación en cliente), `llm_calls` es denso y append-only,
  así que agregar en SQL evita traer todo al browser.
- `GET /metrics/llm/calls` — auditoría: filas crudas con filtros incluir/excluir multi-valor, orden
  por columna (whitelist) y paginación offset (necesita total para el pager "X-Y de N").

El módulo se deriva de `purpose` con un único CASE (`_MODULE_CASE`) reusado en ambos endpoints. El
bucket diario se fija a una TZ (`_BUCKET_TZ`) para que "hoy" no se parta a medianoche (created_at es
TIMESTAMPTZ). `untabulated` (modelo sin precio) se deriva de datos: tokens>0 con cost_usd=0.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import LlmCallList, LlmRollup
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/metrics", tags=["metrics"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.metrics")

#: TZ del bucket diario (locale es-MX del dashboard) — alinea "hoy" con el reloj de pared.
_BUCKET_TZ = "America/Mexico_City"

#: Deriva el módulo/etapa desde `purpose`. El literal `extract_grouped` va ANTES del wildcard
#: `extract_%`; un purpose futuro desconocido cae al ELSE y se ve nombrado (no se pierde el gasto).
_MODULE_CASE = """
    CASE
        WHEN purpose = 'module_route' THEN 'routing'
        WHEN purpose LIKE 'summarize%' THEN 'summarize'
        WHEN purpose = 'extract_grouped' THEN 'grouped'
        WHEN purpose LIKE 'extract_%' THEN substring(purpose FROM 'extract_(.*)')
        WHEN purpose LIKE 'calendar%' THEN 'calendar'
        WHEN purpose = 'ocr' THEN 'ocr'
        ELSE purpose
    END
""".strip()

#: Etiqueta de fuente: nombre real, o pseudo-fuente para las llamadas sin source (calendar cruza
#: fuentes; el resto sin atribución = "(sin source)") — así el gasto sin source se VE, no se pierde.
_SOURCE_LABEL = (
    "COALESCE(s.name, CASE WHEN lc.purpose LIKE 'calendar%' "
    "THEN '(calendar)' ELSE '(sin source)' END)"
)

#: Columnas permitidas para ordenar la auditoría (nunca interpolar input del usuario como columna).
_SORT_COLS = {"created_at": "created_at", "cost_usd": "cost_usd", "latency_ms": "latency_ms"}


def _window(since: datetime | None, until: datetime | None, params: dict[str, Any]) -> str:
    """WHERE de ventana sobre `llm_calls lc` (alias lc). Agrega :since/:until a `params`."""
    clauses = ["lc.user_id = :uid"]
    if since is not None:
        clauses.append("lc.created_at >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("lc.created_at < :until")
        params["until"] = until
    return " AND ".join(clauses)


@router.get("/llm/rollup", response_model=LlmRollup)
async def llm_rollup(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Agregaciones de costo LLM del rango [`since`, `until`) para la vista /metricas."""
    params: dict[str, Any] = {"uid": user_id, "tz": _BUCKET_TZ}
    where = _window(since, until, params)

    with connection() as conn:
        krow = (
            conn.execute(
                text(f"""
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(lc.cost_usd), 0) AS cost_usd,
                    COALESCE(SUM(lc.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(lc.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(lc.cache_hit_tokens), 0) AS cache_hit_tokens,
                    COUNT(*) FILTER (WHERE lc.status = 'error') AS errors,
                    COALESCE(AVG(lc.latency_ms), 0) AS avg_latency_ms
                FROM llm_calls lc
                WHERE {where}
            """),
                params,
            )
            .mappings()
            .one()
        )

        # Periodo anterior de igual longitud, solo si hay `since` (para la variación %).
        prev_cost: float | None = None
        if since is not None:
            until_eff = until if until is not None else datetime.now(UTC)
            span = until_eff - since
            prev_cost = float(
                conn.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                        "WHERE user_id = :uid AND created_at >= :ps AND created_at < :pe"
                    ),
                    {"uid": user_id, "ps": since - span, "pe": since},
                ).scalar_one()
            )

        by_source = (
            conn.execute(
                text(f"""
                SELECT lc.source_id,
                       {_SOURCE_LABEL} AS source_name,
                       COUNT(*) AS calls,
                       COALESCE(SUM(lc.prompt_tokens + lc.completion_tokens), 0) AS tokens,
                       COALESCE(SUM(lc.cost_usd), 0) AS cost_usd
                FROM llm_calls lc
                LEFT JOIN sources s ON s.id = lc.source_id
                WHERE {where}
                GROUP BY lc.source_id, source_name
                ORDER BY cost_usd DESC
            """),
                params,
            )
            .mappings()
            .all()
        )

        by_module = (
            conn.execute(
                text(f"""
                SELECT {_MODULE_CASE} AS module,
                       COUNT(*) AS calls,
                       COALESCE(SUM(lc.prompt_tokens + lc.completion_tokens), 0) AS tokens,
                       COALESCE(SUM(lc.cost_usd), 0) AS cost_usd
                FROM llm_calls lc
                WHERE {where}
                GROUP BY module
                ORDER BY cost_usd DESC
            """),
                params,
            )
            .mappings()
            .all()
        )

        by_model = (
            conn.execute(
                text(f"""
                SELECT lc.model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(lc.prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(lc.completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(lc.cost_usd), 0) AS cost_usd,
                       (COALESCE(SUM(lc.prompt_tokens + lc.completion_tokens), 0) > 0
                        AND COALESCE(SUM(lc.cost_usd), 0) = 0) AS untabulated
                FROM llm_calls lc
                WHERE {where}
                GROUP BY lc.model
                ORDER BY cost_usd DESC
            """),
                params,
            )
            .mappings()
            .all()
        )

        by_source_module = (
            conn.execute(
                text(f"""
                SELECT lc.source_id,
                       {_SOURCE_LABEL} AS source_name,
                       {_MODULE_CASE} AS module,
                       COUNT(*) AS calls,
                       COALESCE(SUM(lc.cost_usd), 0) AS cost_usd
                FROM llm_calls lc
                LEFT JOIN sources s ON s.id = lc.source_id
                WHERE {where}
                GROUP BY lc.source_id, source_name, module
                ORDER BY cost_usd DESC
            """),
                params,
            )
            .mappings()
            .all()
        )

        daily_rows = (
            conn.execute(
                text(f"""
                SELECT (lc.created_at AT TIME ZONE :tz)::date AS day,
                       {_MODULE_CASE} AS module,
                       COALESCE(SUM(lc.cost_usd), 0) AS cost_usd
                FROM llm_calls lc
                WHERE {where}
                GROUP BY day, module
                ORDER BY day
            """),
                params,
            )
            .mappings()
            .all()
        )

    # Pivot de la serie diaria (formato largo → {day, total, by_module}).
    daily_map: dict[str, dict[str, Any]] = {}
    for r in daily_rows:
        day = r["day"].isoformat()
        entry = daily_map.setdefault(day, {"day": day, "total": 0.0, "by_module": {}})
        cost = float(r["cost_usd"])
        entry["by_module"][r["module"]] = cost
        entry["total"] += cost
    daily = [daily_map[d] for d in sorted(daily_map)]

    prompt = int(krow["prompt_tokens"])
    cache_hit = int(krow["cache_hit_tokens"])
    calls = int(krow["calls"])
    cost = float(krow["cost_usd"])
    kpis = {
        "cost_usd": cost,
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": int(krow["completion_tokens"]),
        "cache_hit_tokens": cache_hit,
        "cache_hit_ratio": (cache_hit / prompt) if prompt else 0.0,
        "avg_cost_usd": (cost / calls) if calls else 0.0,
        "avg_latency_ms": float(krow["avg_latency_ms"]),
        "errors": int(krow["errors"]),
        "prev_cost_usd": prev_cost,
    }

    _log.info("metrics.rollup", user_id=user_id, calls=calls, cost_usd=cost)
    return {
        "kpis": kpis,
        "by_source": [
            {
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"]),
                "cost_usd": float(r["cost_usd"]),
            }
            for r in by_source
        ],
        "by_module": [
            {
                "module": r["module"],
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"]),
                "cost_usd": float(r["cost_usd"]),
            }
            for r in by_module
        ],
        "by_model": [
            {
                "model": r["model"],
                "calls": int(r["calls"]),
                "prompt_tokens": int(r["prompt_tokens"]),
                "completion_tokens": int(r["completion_tokens"]),
                "cost_usd": float(r["cost_usd"]),
                "untabulated": bool(r["untabulated"]),
            }
            for r in by_model
        ],
        "by_source_module": [
            {
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "module": r["module"],
                "calls": int(r["calls"]),
                "cost_usd": float(r["cost_usd"]),
            }
            for r in by_source_module
        ],
        "daily": daily,
        "modules": [r["module"] for r in by_module],
    }


def _multi_filter(
    col: str,
    name: str,
    values: list[Any] | None,
    mode: str,
    clauses: list[str],
    params: dict[str, Any],
) -> None:
    """Filtro incluir/excluir multi-valor sobre `col` (= ANY) — vacío/None = sin filtro."""
    if not values:
        return
    params[name] = values
    expr = f"{col} = ANY(:{name})"
    clauses.append(f"NOT ({expr})" if mode == "exclude" else expr)


FilterMode = Literal["include", "exclude"]


@router.get("/llm/calls", response_model=LlmCallList)
async def llm_calls(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    status: Annotated[list[str] | None, Query()] = None,
    status_mode: FilterMode = "include",
    module: Annotated[list[str] | None, Query()] = None,
    module_mode: FilterMode = "include",
    model: Annotated[list[str] | None, Query()] = None,
    model_mode: FilterMode = "include",
    source: Annotated[list[str] | None, Query(description="source_name (incl. pseudo)")] = None,
    source_mode: FilterMode = "include",
    q: str | None = None,
    sort: Literal["created_at", "cost_usd", "latency_ms"] = "created_at",
    dir: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Auditoría de `llm_calls`: filas crudas filtrables/ordenables/paginadas (debug).

    Filtros incluir/excluir multi-valor por estado/módulo/modelo/fuente (aislar o excluir);
    `q` busca substring en inbox/request/model/purpose. `sort`/`dir` salen de una whitelist fija.
    """
    params: dict[str, Any] = {"uid": user_id, "limit": limit, "offset": offset}
    where = _window(since, until, params)

    filters: list[str] = []
    _multi_filter("status", "status", status, status_mode, filters, params)
    _multi_filter("module", "module", module, module_mode, filters, params)
    _multi_filter("model", "model", model, model_mode, filters, params)
    _multi_filter("source_name", "source", source, source_mode, filters, params)
    if q:
        params["q"] = f"%{q}%"
        filters.append(
            "(CAST(inbox_id AS TEXT) ILIKE :q OR request_id ILIKE :q "
            "OR model ILIKE :q OR purpose ILIKE :q)"
        )
    filter_sql = (" WHERE " + " AND ".join(filters)) if filters else ""

    col = _SORT_COLS[sort]
    direction = "ASC" if dir == "asc" else "DESC"

    sql = f"""
        WITH c AS (
            SELECT lc.id, lc.created_at, lc.purpose, {_MODULE_CASE} AS module, lc.model,
                   lc.prompt_tokens, lc.completion_tokens, lc.cache_hit_tokens, lc.cost_usd,
                   lc.latency_ms, lc.status, lc.error_message, lc.inbox_id, lc.source_id,
                   lc.request_id, lc.metadata,
                   {_SOURCE_LABEL} AS source_name
            FROM llm_calls lc
            LEFT JOIN sources s ON s.id = lc.source_id
            WHERE {where}
        )
        SELECT id, created_at, purpose, module, model, prompt_tokens, completion_tokens,
               cache_hit_tokens, cost_usd, latency_ms, status, error_message, inbox_id,
               source_id, source_name, metadata, COUNT(*) OVER() AS total
        FROM c{filter_sql}
        ORDER BY {col} {direction}, id {direction}
        LIMIT :limit OFFSET :offset
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    total = int(rows[0]["total"]) if rows else 0
    items = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "purpose": r["purpose"],
            "module": r["module"],
            "model": r["model"],
            "prompt_tokens": int(r["prompt_tokens"]),
            "completion_tokens": int(r["completion_tokens"]),
            "cache_hit_tokens": int(r["cache_hit_tokens"]),
            "cost_usd": float(r["cost_usd"]),
            "latency_ms": int(r["latency_ms"]),
            "status": r["status"],
            "error_message": r["error_message"],
            "inbox_id": r["inbox_id"],
            "source_id": r["source_id"],
            "source_name": r["source_name"],
            "metadata": r["metadata"],
        }
        for r in rows
    ]
    return {"items": items, "total": total}
