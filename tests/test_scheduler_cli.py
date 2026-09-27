"""CLI del scheduler: `run <job>` (one-shot) y `daemon start` (boot)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.scheduler import cli
from memex.scheduler.jobs import Job


def test_run_subcommand_runs_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_run(uid: int) -> None:
        calls.append(uid)

    fake = {"classify": Job("classify", "PT1H", fake_run)}
    monkeypatch.setattr("memex.scheduler.cli.all_jobs", lambda: fake)

    rc = cli.main(["run", "classify", "--user", "1"])
    assert rc == 0
    assert calls == [1]

    with connection() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM worker_runs WHERE job = 'classify' AND status = 'ok'")
        ).scalar_one()
    assert n == 1


def test_run_subcommand_records_error_and_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(uid: int) -> None:
        raise RuntimeError("kaboom")

    fake = {"classify": Job("classify", "PT1H", boom)}
    monkeypatch.setattr("memex.scheduler.cli.all_jobs", lambda: fake)

    rc = cli.main(["run", "classify"])
    assert rc == 1

    with connection() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status, error FROM worker_runs WHERE job = 'classify' ORDER BY id DESC"
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["status"] == "error"
    assert "kaboom" in row["error"]


def test_daemon_start_boots_and_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_forever(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "memex.scheduler.cli.AsyncScheduler.install_signal_handlers", lambda self: None
    )
    monkeypatch.setattr("memex.scheduler.cli.AsyncScheduler.run_forever", fake_run_forever)

    rc = cli.main(["daemon", "start"])
    assert rc == 0
