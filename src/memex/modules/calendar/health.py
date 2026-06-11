"""Salud de la sincronización de calendario: UNA fuente para la UI y el CLI `sync-status`.

Responde la pregunta del dueño «¿está funcionando?» en términos operables, sin jerga interna:
por cuenta, la última bajada (pull) y subida (push) con su edad y resultado, el estado del cursor
delta en llano, y si la sincronización AUTOMÁTICA está activa (daemon del scheduler + job
`calendar` habilitado — espejo de GET /processing/scheduler). El `overall` se decide acá
(server-side) para que dashboard y CLI nunca cuenten historias distintas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Una bajada más vieja que esto ya no cuenta como «funcionando» (overall pasa a desactualizado).
STALE_AFTER_HOURS = 24.0


def _age_hours(ts: datetime | None, *, now: datetime) -> float | None:
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 3600.0)


def _overall(accounts: list[dict[str, Any]]) -> str:
    enabled = [a for a in accounts if a["enabled"]]
    if not enabled:
        return "sin_cuentas"
    if any(a["last_pull_status"] == "error" for a in enabled):
        return "error"
    ages = [a["last_pull_age_hours"] for a in enabled if a["last_pull_age_hours"] is not None]
    if not ages:
        return "nunca"
    return "ok" if min(ages) < STALE_AFTER_HOURS else "desactualizado"


def sync_health(conn: Connection, user_id: int) -> dict[str, Any]:
    """Arma el estado de salud completo (shape de `CalendarSyncHealth`). Read-only."""
    now = datetime.now(UTC)
    account_rows = (
        conn.execute(
            text(
                """
                SELECT id, provider, account_label, enabled, write_back, last_sync_at,
                       (sync_token IS NOT NULL) AS has_cursor
                FROM mod_calendar_provider_accounts
                WHERE user_id = :uid
                ORDER BY id
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    last_runs = {
        (int(r["provider_account_id"]), str(r["direction"])): r
        for r in conn.execute(
            text(
                """
                SELECT DISTINCT ON (provider_account_id, direction)
                       provider_account_id, direction, status, started_at
                FROM mod_calendar_sync_runs
                WHERE user_id = :uid AND provider_account_id IS NOT NULL
                ORDER BY provider_account_id, direction, id DESC
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    }

    accounts: list[dict[str, Any]] = []
    for a in account_rows:
        aid = int(a["id"])
        if a["has_cursor"]:
            cursor_state = "incremental"
        elif a["last_sync_at"] is not None:
            cursor_state = "full_resync_pendiente"
        else:
            cursor_state = "sin_primera_sync"
        pull = last_runs.get((aid, "ingress"))
        push = last_runs.get((aid, "egress"))
        accounts.append(
            {
                "account_id": aid,
                "provider": str(a["provider"]),
                "account_label": str(a["account_label"]),
                "enabled": bool(a["enabled"]),
                "write_back": bool(a["write_back"]),
                "cursor_state": cursor_state,
                "last_pull_at": pull["started_at"] if pull else None,
                "last_pull_status": str(pull["status"]) if pull else None,
                "last_pull_age_hours": _age_hours(pull["started_at"] if pull else None, now=now),
                "last_push_at": push["started_at"] if push else None,
                "last_push_status": str(push["status"]) if push else None,
            }
        )

    srow = (
        conn.execute(
            text(
                "SELECT daemon_enabled, enabled_jobs FROM scheduler_settings WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    daemon_enabled = bool(srow["daemon_enabled"]) if srow else False
    enabled_jobs = {
        j.strip() for j in str(srow["enabled_jobs"] if srow else "").split(",") if j.strip()
    }
    calendar_job_enabled = "calendar" in enabled_jobs
    last_cycle = conn.execute(
        text(
            "SELECT started_at FROM worker_runs "
            "WHERE user_id = :uid AND run_type = 'job' AND job = 'calendar' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"uid": user_id},
    ).scalar()

    return {
        "overall": _overall(accounts),
        "auto_sync_active": daemon_enabled and calendar_job_enabled,
        "daemon_enabled": daemon_enabled,
        "calendar_job_enabled": calendar_job_enabled,
        "last_cycle_at": last_cycle,
        "accounts": accounts,
    }
