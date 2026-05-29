from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text

from memex.core.observability import ingestion_run, record_llm_call
from memex.db import connection
from memex.ingestors.runner import RunStats
from memex.logging import clear_request_context


def _last_run(uid: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text(
                    """
                    SELECT id, status, posted, inserted, duplicates, errors,
                           filtered, error_class, error_message, duration_ms, trigger
                    FROM ingestion_runs
                    WHERE user_id = :uid
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ),
                {"uid": uid},
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


def test_ingestion_run_happy_path_marks_status_ok(seed_source: dict[str, Any]) -> None:
    sid = int(seed_source["id"])
    uid = int(seed_source["user_id"])

    with ingestion_run(user_id=uid, source_id=sid, trigger="test") as run:
        run.finalize(
            RunStats(posted=7, inserted=4, duplicates=1, errors=0, filtered=2, ms_elapsed=123)
        )

    row = _last_run(uid)
    assert row["status"] == "ok"
    assert row["posted"] == 7
    assert row["inserted"] == 4
    assert row["duplicates"] == 1
    assert row["errors"] == 0
    assert row["filtered"] == 2
    # Invariante: posted = inserted + duplicates + errors + filtered.
    assert row["posted"] == row["inserted"] + row["duplicates"] + row["errors"] + row["filtered"]
    assert row["error_class"] is None
    assert row["error_message"] is None
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["trigger"] == "test"


def test_ingestion_run_fail_marks_status_failed(seed_source: dict[str, Any]) -> None:
    sid = int(seed_source["id"])
    uid = int(seed_source["user_id"])

    with ingestion_run(user_id=uid, source_id=sid, trigger="test") as run:
        run.fail(ValueError("boom"))

    row = _last_run(uid)
    assert row["status"] == "failed"
    assert row["error_class"] == "ValueError"
    assert row["error_message"] == "boom"


def test_ingestion_run_without_settle_marks_aborted(seed_source: dict[str, Any]) -> None:
    sid = int(seed_source["id"])
    uid = int(seed_source["user_id"])

    with ingestion_run(user_id=uid, source_id=sid, trigger="test"):
        pass

    row = _last_run(uid)
    assert row["status"] == "aborted"


def test_ingestion_run_binds_and_unbinds_contextvars(seed_source: dict[str, Any]) -> None:
    sid = int(seed_source["id"])
    uid = int(seed_source["user_id"])
    clear_request_context()

    with ingestion_run(user_id=uid, source_id=sid, trigger="test") as run:
        bound = structlog.contextvars.get_contextvars()
        assert bound.get("run_id") == run.id
        assert bound.get("source_id") == sid
        assert bound.get("user_id") == uid
        run.finalize(RunStats())

    bound_after = structlog.contextvars.get_contextvars()
    assert "run_id" not in bound_after
    assert "source_id" not in bound_after
    assert "user_id" not in bound_after


def test_ingestion_run_double_settle_is_noop(seed_source: dict[str, Any]) -> None:
    sid = int(seed_source["id"])
    uid = int(seed_source["user_id"])

    with ingestion_run(user_id=uid, source_id=sid, trigger="test") as run:
        run.finalize(RunStats(posted=3, inserted=3))
        run.fail(RuntimeError("late"))

    row = _last_run(uid)
    assert row["status"] == "ok"
    assert row["posted"] == 3


def test_record_llm_call_inserts_row(seed_source: dict[str, Any]) -> None:
    uid = int(seed_source["user_id"])

    row_id = record_llm_call(
        user_id=uid,
        purpose="summarize_individual",
        model="deepseek-v3.2",
        prompt_tokens=120,
        completion_tokens=80,
        cost_usd=Decimal("0.000234"),
        latency_ms=950,
        status="ok",
    )

    with connection() as c:
        row = (
            c.execute(
                text(
                    """
                    SELECT user_id, purpose, model, prompt_tokens,
                           completion_tokens, cost_usd, latency_ms, status,
                           request_id, inbox_id
                    FROM llm_calls WHERE id = :id
                    """
                ),
                {"id": row_id},
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["user_id"] == uid
    assert row["purpose"] == "summarize_individual"
    assert row["model"] == "deepseek-v3.2"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 80
    assert row["latency_ms"] == 950
    assert row["status"] == "ok"
    assert row["request_id"] is None
    assert row["inbox_id"] is None


def test_record_llm_call_picks_up_request_id_from_contextvars(
    seed_source: dict[str, Any],
) -> None:
    from memex.logging import bind_request_context

    uid = int(seed_source["user_id"])
    clear_request_context()
    bind_request_context(request_id="req-llm-1")
    try:
        row_id = record_llm_call(
            user_id=uid,
            purpose="summarize_batch",
            model="deepseek-v3.2",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=Decimal("0.000010"),
            latency_ms=200,
            status="ok",
        )
    finally:
        clear_request_context()

    with connection() as c:
        rid = c.execute(
            text("SELECT request_id FROM llm_calls WHERE id = :id"),
            {"id": row_id},
        ).scalar()
    assert rid == "req-llm-1"
