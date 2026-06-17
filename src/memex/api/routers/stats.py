"""Observabilidad del pipeline para el dashboard (vistas /pipeline y /resumen).

Solo lectura: agrega server-side las tablas de observabilidad que ya existen y que hasta ahora el
dashboard solo mostraba con datos mock.

- `GET /stats/pipeline` — salud por fuente (última corrida + success rate + insertados/filtrados +
  sparkline), estado del último run de cada worker del scheduler (+ stale) y las corridas de ingesta
  recientes con el invariante `posted = inserted + duplicates + errors + filtered` y sus totales.
- `GET /stats/overview` — los contadores del /resumen: pendientes de revisión (dead-letter +
  conflictos de calendar), inbox sin procesar, inbox con error y workers colgados.

`since`/`until` acotan SOLO el panel de corridas de ingesta. La salud por fuente y el estado de
workers no dependen del rango (es "lo más reciente / de por vida"), igual que los selectores mock
que reemplazan. NO escribe nada: administrar el pipeline (toggles) es otro slice.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import StatsAlert, StatsOverview, StatsPipeline
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/stats", tags=["stats"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.stats")

#: Jobs del scheduler server-side (memex-scheduler), en orden estable para la UI. Un job desconocido
#: que aparezca en worker_runs igual se muestra (se anexa al final) — no se oculta actividad.
_JOBS = ("classify", "extract", "ocr", "calendar", "log_purge")

#: Última corrida 'running' pasado este umbral = worker colgado (daemon muerto).
_STALE = "interval '30 minutes'"


def _sparkline(rows: list[Any], source_id: int) -> list[dict[str, Any]]:
    return [
        {"started_at": r["started_at"], "inserted": int(r["inserted"])}
        for r in rows
        if r["source_id"] == source_id
    ]


@router.get("/pipeline", response_model=StatsPipeline)
async def pipeline(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Salud por fuente + estado de workers + corridas de ingesta recientes (con invariante)."""
    with connection() as conn:
        # --- Fuentes: lista + agregados de por vida + última corrida + sparkline ------------------
        sources = (
            conn.execute(
                text("""
                SELECT s.id, s.name, s.type, s.enabled, a.alias,
                       COALESCE(s.config->>'account_email', a.metadata->>'email') AS account_email
                FROM sources s
                LEFT JOIN accounts a ON a.id = s.account_id
                WHERE s.user_id = :uid
                ORDER BY s.id
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )

        agg = {
            r["source_id"]: r
            for r in conn.execute(
                text("""
                SELECT source_id,
                       COUNT(*) FILTER (WHERE status IN ('ok','failed','aborted')) AS finished,
                       COUNT(*) FILTER (WHERE status = 'ok') AS ok,
                       COALESCE(SUM(inserted), 0) AS total_inserted,
                       COALESCE(SUM(filtered), 0) AS total_filtered
                FROM ingestion_runs
                WHERE user_id = :uid
                GROUP BY source_id
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        }

        last_runs = {
            r["source_id"]: r
            for r in conn.execute(
                text("""
                SELECT DISTINCT ON (source_id)
                       source_id, started_at, ended_at, status, error_class, error_message
                FROM ingestion_runs
                WHERE user_id = :uid
                ORDER BY source_id, started_at DESC
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        }

        spark = (
            conn.execute(
                text("""
                SELECT source_id, started_at, inserted
                FROM (
                    SELECT source_id, started_at, inserted,
                           ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY started_at DESC) AS rn
                    FROM ingestion_runs
                    WHERE user_id = :uid
                ) t
                WHERE rn <= 10
                ORDER BY source_id, started_at
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )

        # --- Workers: último run por job + si está colgado ----------------------------------------
        worker_rows = {
            r["job"]: r
            for r in conn.execute(
                text(f"""
                SELECT DISTINCT ON (job)
                       job, started_at, finished_at, status, stats, error,
                       (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
                FROM worker_runs
                WHERE user_id = :uid AND run_type = 'job'
                ORDER BY job, started_at DESC
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        }

        # --- Corridas de ingesta recientes (acotadas por since/until) -----------------------------
        run_where = ["r.user_id = :uid"]
        run_params: dict[str, Any] = {"uid": user_id, "limit": limit}
        if since is not None:
            run_where.append("r.started_at >= :since")
            run_params["since"] = since
        if until is not None:
            run_where.append("r.started_at < :until")
            run_params["until"] = until
        run_rows = (
            conn.execute(
                text(f"""
                SELECT r.id, r.source_id, s.name AS source_name, r.trigger, r.status,
                       r.started_at, r.ended_at, r.posted, r.inserted, r.duplicates,
                       r.errors, r.filtered, r.error_class, r.error_message, r.api_cost_usd,
                       a.alias AS account_alias,
                       COALESCE(s.config->>'account_email', a.metadata->>'email') AS account_email
                FROM ingestion_runs r
                LEFT JOIN sources s ON s.id = r.source_id
                LEFT JOIN accounts a ON a.id = s.account_id
                WHERE {" AND ".join(run_where)}
                ORDER BY r.started_at DESC
                LIMIT :limit
            """),
                run_params,
            )
            .mappings()
            .all()
        )

    # --- Ensamblado de fuentes ----------------------------------------------------------------
    source_items: list[dict[str, Any]] = []
    for s in sources:
        sid = s["id"]
        a = agg.get(sid)
        finished = int(a["finished"]) if a else 0
        ok = int(a["ok"]) if a else 0
        lr = last_runs.get(sid)
        source_items.append(
            {
                "source_id": sid,
                "name": s["name"],
                "type": s["type"],
                "enabled": bool(s["enabled"]),
                "alias": s["alias"],
                "account_email": s["account_email"],
                "last_run": (
                    {
                        "started_at": lr["started_at"],
                        "ended_at": lr["ended_at"],
                        "status": lr["status"],
                        "error_class": lr["error_class"],
                        "error_message": lr["error_message"],
                    }
                    if lr
                    else None
                ),
                "success_rate": (ok / finished) if finished else 0.0,
                "total_inserted": int(a["total_inserted"]) if a else 0,
                "total_filtered": int(a["total_filtered"]) if a else 0,
                "recent": _sparkline(list(spark), sid),
            }
        )

    # --- Ensamblado de workers (lista fija + jobs extra al final) ------------------------------
    extra_jobs = sorted(j for j in worker_rows if j not in _JOBS)
    worker_items: list[dict[str, Any]] = []
    for job in (*_JOBS, *extra_jobs):
        w = worker_rows.get(job)
        worker_items.append(
            {
                "job": job,
                "latest": (
                    {
                        "started_at": w["started_at"],
                        "finished_at": w["finished_at"],
                        "status": w["status"],
                        "stats": w["stats"],
                        "error": w["error"],
                    }
                    if w
                    else None
                ),
                "is_stale": bool(w["is_stale"]) if w else False,
            }
        )

    # --- Ensamblado de corridas de ingesta + invariante + totales ------------------------------
    runs: list[dict[str, Any]] = []
    totals = {k: 0 for k in ("posted", "inserted", "duplicates", "errors", "filtered")}
    unbalanced = 0
    api_cost_total = 0.0
    for r in run_rows:
        expected = int(r["inserted"]) + int(r["duplicates"]) + int(r["errors"]) + int(r["filtered"])
        balanced = int(r["posted"]) == expected
        if not balanced:
            unbalanced += 1
        for k in totals:
            totals[k] += int(r[k])
        api_cost = float(r["api_cost_usd"]) if r["api_cost_usd"] is not None else None
        if api_cost is not None:
            api_cost_total += api_cost
        runs.append(
            {
                "id": str(r["id"]),
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "account_alias": r["account_alias"],
                "account_email": r["account_email"],
                "trigger": r["trigger"],
                "status": r["status"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "posted": int(r["posted"]),
                "inserted": int(r["inserted"]),
                "duplicates": int(r["duplicates"]),
                "errors": int(r["errors"]),
                "filtered": int(r["filtered"]),
                "error_class": r["error_class"],
                "error_message": r["error_message"],
                "api_cost_usd": api_cost,
                "expected": expected,
                "balanced": balanced,
            }
        )

    _log.info(
        "stats.pipeline",
        user_id=user_id,
        sources=len(source_items),
        runs=len(runs),
    )
    return {
        "sources": source_items,
        "workers": worker_items,
        "ingestion": {
            "runs": runs,
            "totals": {
                **totals,
                "runs": len(runs),
                "unbalanced": unbalanced,
                "api_cost_usd": api_cost_total,
            },
        },
    }


@router.get("/overview", response_model=StatsOverview)
async def overview(user_id: UserID) -> dict[str, Any]:
    """Contadores del /resumen: pendientes de revisión, inbox sin procesar/con error y workers
    colgados."""
    with connection() as conn:
        dead_letter = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM work_item_failures "
                    "WHERE user_id = :uid AND status = 'review'"
                ),
                {"uid": user_id},
            ).scalar_one()
        )
        calendar_conflicts = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM mod_calendar_conflicts "
                    "WHERE user_id = :uid AND status = 'pending'"
                ),
                {"uid": user_id},
            ).scalar_one()
        )
        inbox_counts = (
            conn.execute(
                text("""
            SELECT
                COUNT(*) FILTER (WHERE processed_at IS NULL AND process_error IS NULL) AS pending,
                COUNT(*) FILTER (WHERE process_error IS NOT NULL) AS errors
            FROM inbox
            WHERE user_id = :uid
        """),
                {"uid": user_id},
            )
            .mappings()
            .one()
        )
        stale_workers = int(
            conn.execute(
                text(f"""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT ON (job) status, started_at
                    FROM worker_runs
                    WHERE user_id = :uid AND run_type = 'job'
                    ORDER BY job, started_at DESC
                ) latest
                WHERE status = 'running' AND NOW() - started_at > {_STALE}
            """),
                {"uid": user_id},
            ).scalar_one()
        )

    _log.info(
        "stats.overview",
        user_id=user_id,
        dead_letter=dead_letter,
        calendar_conflicts=calendar_conflicts,
    )
    return {
        "review": {
            "dead_letter": dead_letter,
            "calendar_conflicts": calendar_conflicts,
            "total": dead_letter + calendar_conflicts,
        },
        "inbox_pending": int(inbox_counts["pending"]),
        "inbox_errors": int(inbox_counts["errors"]),
        "stale_workers": stale_workers,
    }


#: Marcadores en el mensaje de error que delatan un problema de saldo/cuota del LLM (402).
_QUOTA_MARKERS = ("402", "saldo", "quota", "insufficient")
#: Orden de severidad para que las más graves salgan primero (la UI muestra el top-5).
_SEV_RANK = {"critica": 0, "alta": 1, "info": 2}


@router.get("/alerts", response_model=list[StatsAlert])
async def alerts(user_id: UserID) -> list[dict[str, Any]]:
    """Alertas REALES derivadas de la observabilidad (reemplaza el seed mock): fuentes cuya ÚLTIMA
    ingesta falló, workers colgados o con error (saldo si el error lo delata) y backlog de revisión.
    Lista vacía = todo en orden."""
    out: list[dict[str, Any]] = []
    with connection() as conn:
        # 1. Fuentes cuya ÚLTIMA corrida de ingesta falló/abortó.
        failed = (
            conn.execute(
                text("""
                SELECT lr.source_id, s.name, lr.started_at, lr.error_class, lr.error_message
                FROM (
                    SELECT DISTINCT ON (source_id) source_id, status, started_at,
                           error_class, error_message
                    FROM ingestion_runs WHERE user_id = :uid
                    ORDER BY source_id, started_at DESC
                ) lr
                JOIN sources s ON s.id = lr.source_id
                WHERE lr.status IN ('failed', 'aborted')
                ORDER BY lr.source_id
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
        for r in failed:
            out.append(
                {
                    "id": f"run-{r['source_id']}",
                    "severity": "alta",
                    "kind": "run-failed",
                    "title": f"Ingesta fallida: {r['name']}",
                    "detail": str(
                        r["error_message"] or r["error_class"] or "la última corrida falló"
                    ),
                    "at": r["started_at"],
                    "read": False,
                    "deep_link": "/pipeline",
                }
            )

        # 2. Workers: colgados (running > umbral) o con la última corrida en error.
        workers = (
            conn.execute(
                text(f"""
                SELECT DISTINCT ON (job) job, status, started_at, error,
                       (status = 'running' AND NOW() - started_at > {_STALE}) AS is_stale
                FROM worker_runs
                WHERE user_id = :uid AND run_type = 'job'
                ORDER BY job, started_at DESC
            """),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
        for w in workers:
            if w["is_stale"]:
                out.append(
                    {
                        "id": f"stale-{w['job']}",
                        "severity": "alta",
                        "kind": "worker-stale",
                        "title": f"Worker {w['job']} colgado",
                        "detail": "lleva más de 30 min en 'running' — posible daemon caído",
                        "at": w["started_at"],
                        "read": False,
                        "deep_link": "/pipeline",
                    }
                )
            elif w["status"] == "error":
                err = str(w["error"] or "")
                quota = any(m in err.lower() for m in _QUOTA_MARKERS)
                out.append(
                    {
                        "id": f"worker-{w['job']}",
                        "severity": "critica" if quota else "alta",
                        "kind": "saldo" if quota else "run-failed",
                        "title": "Saldo/cuota LLM agotado" if quota else f"Worker {w['job']} falló",
                        "detail": err or "la última corrida terminó en error",
                        "at": w["started_at"],
                        "read": False,
                        "deep_link": "/pipeline",
                    }
                )

        # 3. Backlog de revisión (dead-letter + conflictos de calendario pendientes).
        review = (
            conn.execute(
                text("""
                SELECT
                  (SELECT COUNT(*) FROM work_item_failures
                   WHERE user_id = :uid AND status = 'review') AS dl,
                  (SELECT COUNT(*) FROM mod_calendar_conflicts
                   WHERE user_id = :uid AND status = 'pending') AS cf,
                  GREATEST(
                    (SELECT MAX(updated_at) FROM work_item_failures
                     WHERE user_id = :uid AND status = 'review'),
                    (SELECT MAX(created_at) FROM mod_calendar_conflicts
                     WHERE user_id = :uid AND status = 'pending')
                  ) AS at
            """),
                {"uid": user_id},
            )
            .mappings()
            .one()
        )
        total = int(review["dl"]) + int(review["cf"])
        if total > 0:
            out.append(
                {
                    "id": "review",
                    "severity": "info",
                    "kind": "review",
                    "title": f"{total} ítems pendientes de revisión",
                    "detail": "Dead-letter + conflictos de calendario esperan tu decisión.",
                    "at": review["at"],
                    "read": False,
                    "deep_link": "/revision",
                }
            )

    out.sort(key=lambda a: _SEV_RANK[str(a["severity"])])
    _log.info("stats.alerts", user_id=user_id, count=len(out))
    return out
