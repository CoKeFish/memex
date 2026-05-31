"""Persistencia de worker_runs contra la DB de test (start_run / finish_run)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.classifier.worker import ClassifyStats
from memex.scheduler import runs


def test_start_run_inserts_running_row(conn: Any) -> None:
    run_id = runs.start_run(1, "classify")
    row = (
        conn.execute(
            text("SELECT user_id, job, status, finished_at FROM worker_runs WHERE id = :id"),
            {"id": run_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["user_id"] == 1
    assert row["job"] == "classify"
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_finish_run_ok_persists_stats_json(conn: Any) -> None:
    run_id = runs.start_run(1, "classify")
    runs.finish_run(
        run_id,
        status="ok",
        stats=ClassifyStats(scanned=3, classified=2, by_tier={"batch": 2}),
    )
    row = (
        conn.execute(
            text(
                "SELECT status, finished_at, stats->>'classified' AS classified "
                "FROM worker_runs WHERE id = :id"
            ),
            {"id": run_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    assert row["classified"] == "2"


def test_finish_run_error_records_message(conn: Any) -> None:
    run_id = runs.start_run(1, "summarize")
    runs.finish_run(run_id, status="error", error="boom")
    row = (
        conn.execute(
            text("SELECT status, error, stats FROM worker_runs WHERE id = :id"),
            {"id": run_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["status"] == "error"
    assert row["error"] == "boom"
    assert row["stats"] == {}  # sin stats → '{}'
