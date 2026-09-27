"""Alertas de OBSERVABILIDAD de memex (lo interno: cuentas con credenciales caídas, ingestas
fallidas, workers colgados o en error, saldo LLM agotado, backlog de revisión). Cálculo puro
sobre una `Connection`; lo sirven
`GET /stats/alerts` (campana del dashboard) y el productor `sistema` del daemon de notificaciones
(audiencia `sistema`). Misma forma que `StatsAlert`: id estable por condición, severidad
(info|alta|critica), kind, title, detail, at, deep_link. `dedup` (opcional) es la clave con la
que el productor `sistema` evita repetir el aviso (default: id + instante).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

#: Un worker en 'running' más de esto se considera colgado (daemon caído a media corrida).
_STALE = "interval '30 minutes'"
#: Marcadores en el mensaje de error que delatan un problema de saldo/cuota del LLM (402).
_QUOTA_MARKERS = ("402", "saldo", "quota", "insufficient")
#: Orden de severidad para que las más graves salgan primero.
SEV_RANK = {"critica": 0, "alta": 1, "info": 2}


def compute_alerts(conn: Connection, user_id: int) -> list[dict[str, Any]]:
    """Alertas REALES derivadas de la observabilidad: fuentes cuya ÚLTIMA ingesta falló, workers
    colgados o con error (saldo si el error lo delata) y backlog de revisión. Vacía = en orden.
    Ordenadas por severidad (críticas primero)."""
    out: list[dict[str, Any]] = []
    # 0. Cuentas cuyas credenciales ya NO sirven según la evidencia más reciente (chequeo o
    #    ingesta). Un aviso por cuenta y por día mientras siga caída (`dedup`), aunque el daemon
    #    la revalide cada pocas horas.
    from memex.core.account_health import effective_health

    accounts = (
        conn.execute(
            text("""
            SELECT id, alias, provider, health_status, last_health_check_at, health_detail,
                   health_code
            FROM accounts WHERE user_id = :uid AND enabled ORDER BY id
        """),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    for a in accounts:
        eh = effective_health(
            conn, int(a["id"]), str(a["health_status"]), a["last_health_check_at"]
        )
        if eh.status not in ("unhealthy", "degraded"):
            continue
        at = eh.evidence_at
        day = at.date().isoformat() if at is not None else "na"
        detail = (
            str(a["health_detail"] or "")
            if eh.basis == "check"
            else "la última ingesta de esta cuenta falló"
        )
        if a["health_code"] == "reauth_required" and eh.basis == "check":
            detail += " → «Reconectar con Google» en /cuenta."
        out.append(
            {
                "id": f"account-{a['id']}",
                # Crítica: sin credenciales no entra NADA de esa cuenta hasta que el usuario actúe.
                "severity": "critica",
                "kind": "account-unhealthy",
                "title": f"Cuenta con error: {a['alias']} ({a['provider']})",
                "detail": detail or "las credenciales no funcionan",
                "at": at,
                "read": False,
                "deep_link": "/cuenta",
                "dedup": f"account-{a['id']}:{a['health_code'] or eh.status}:{day}",
            }
        )

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
                "detail": str(r["error_message"] or r["error_class"] or "la última corrida falló"),
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

    out.sort(key=lambda a: SEV_RANK[str(a["severity"])])
    return out
