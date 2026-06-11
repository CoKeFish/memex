"""Salud de la sync de calendario (`sync_health`): overall, cursor, automatización, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.cli import main as cal_main
from memex.modules.calendar.health import sync_health


def _seed_account(
    *,
    enabled: bool = True,
    write_back: bool = False,
    sync_token: str | None = None,
    last_sync_at: datetime | None = None,
    label: str = "user@gmail.com",
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_provider_accounts
                      (user_id, provider, account_label, token_path_env, enabled, write_back,
                       sync_token, last_sync_at)
                    VALUES (1, 'google', :label, 'GOOGLE_CALENDAR_TOKEN_PATH', :en, :wb, :tok,
                            :ls)
                    RETURNING id
                    """
                ),
                {
                    "label": label,
                    "en": enabled,
                    "wb": write_back,
                    "tok": sync_token,
                    "ls": last_sync_at,
                },
            ).scalar_one()
        )


def _seed_run(
    account_id: int,
    *,
    direction: str = "ingress",
    status: str = "ok",
    hours_ago: float = 1.0,
) -> None:
    started = datetime.now(UTC) - timedelta(hours=hours_ago)
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_sync_runs
                  (user_id, provider_account_id, direction, status, started_at, finished_at)
                VALUES (1, :aid, :dir, :st, :ts, :ts)
                """
            ),
            {"aid": account_id, "dir": direction, "st": status, "ts": started},
        )


def _seed_scheduler(*, daemon_enabled: bool, enabled_jobs: str) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO scheduler_settings (user_id, daemon_enabled, enabled_jobs)
                VALUES (1, :d, :j)
                ON CONFLICT (user_id) DO UPDATE
                  SET daemon_enabled = EXCLUDED.daemon_enabled,
                      enabled_jobs = EXCLUDED.enabled_jobs
                """
            ),
            {"d": daemon_enabled, "j": enabled_jobs},
        )


def _health() -> dict[str, Any]:
    with connection() as c:
        return sync_health(c, 1)


def test_no_accounts() -> None:
    data = _health()
    assert data["overall"] == "sin_cuentas"
    assert data["accounts"] == []
    assert data["auto_sync_active"] is False


def test_never_synced_account() -> None:
    _seed_account()
    data = _health()
    assert data["overall"] == "nunca"
    acc = data["accounts"][0]
    assert acc["cursor_state"] == "sin_primera_sync"
    assert acc["last_pull_at"] is None


def test_fresh_pull_is_ok_and_incremental() -> None:
    aid = _seed_account(sync_token="tok", last_sync_at=datetime.now(UTC))
    _seed_run(aid, hours_ago=2.0)
    data = _health()
    assert data["overall"] == "ok"
    acc = data["accounts"][0]
    assert acc["cursor_state"] == "incremental"
    assert acc["last_pull_status"] == "ok"
    assert 1.5 < acc["last_pull_age_hours"] < 3.0


def test_old_pull_is_stale() -> None:
    aid = _seed_account(last_sync_at=datetime.now(UTC) - timedelta(days=7))
    _seed_run(aid, hours_ago=7 * 24.0)
    data = _health()
    assert data["overall"] == "desactualizado"
    assert data["accounts"][0]["cursor_state"] == "full_resync_pendiente"


def test_error_pull_dominates() -> None:
    aid = _seed_account(sync_token="tok")
    _seed_run(aid, hours_ago=1.0, status="error")
    assert _health()["overall"] == "error"


def test_disabled_account_ignored_for_overall() -> None:
    aid = _seed_account(enabled=False)
    _seed_run(aid, hours_ago=1.0, status="error")
    assert _health()["overall"] == "sin_cuentas"


def test_push_tracked_separately() -> None:
    aid = _seed_account(write_back=True, sync_token="tok")
    _seed_run(aid, direction="ingress", hours_ago=1.0)
    _seed_run(aid, direction="egress", hours_ago=0.5)
    acc = _health()["accounts"][0]
    assert acc["last_push_status"] == "ok"
    assert acc["last_push_at"] is not None


def test_auto_sync_requires_daemon_and_job() -> None:
    _seed_account()
    _seed_scheduler(daemon_enabled=True, enabled_jobs="classify,summarize")
    data = _health()
    assert data["daemon_enabled"] is True
    assert data["calendar_job_enabled"] is False
    assert data["auto_sync_active"] is False

    _seed_scheduler(daemon_enabled=True, enabled_jobs="classify,calendar")
    data = _health()
    assert data["auto_sync_active"] is True
    assert data["calendar_job_enabled"] is True


def test_last_cycle_from_worker_runs() -> None:
    _seed_account()
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO worker_runs (user_id, job, status, run_type, started_at) "
                "VALUES (1, 'calendar', 'ok', 'job', NOW() - interval '30 minutes')"
            )
        )
    assert _health()["last_cycle_at"] is not None


def test_cli_sync_status_json(capsys: pytest.CaptureFixture[str]) -> None:
    aid = _seed_account(sync_token="tok", write_back=True)
    _seed_run(aid, hours_ago=3.0)
    rc = cal_main(["sync-status", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["overall"] == "ok"
    assert data["accounts"][0]["cursor_state"] == "incremental"


def test_cli_sync_status_human(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_account()
    rc = cal_main(["sync-status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUNCA" in out
    assert "APAGADA" in out  # sin scheduler: la sync automática está apagada
