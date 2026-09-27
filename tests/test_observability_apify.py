"""record_apify_runs — single-writer de `apify_runs` + agregado en `ingestion_runs`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from memex.core.observability import record_apify_runs
from memex.core.source import ActorRunReport
from memex.db import connection


def _report(**overrides: Any) -> ActorRunReport:
    base: dict[str, Any] = {
        "platform": "x",
        "account": "nasa",
        "actor_id": "apidojo/tweet-scraper",
        "apify_run_id": "RUN1",
        "status": "ok",
        "items_scraped": 5,
        "items_kept": 3,
        "cost_usd": 0.0021,
        "charged_events": {"apify-default-dataset-item": 5},
        "started_at": datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 6, 10, 12, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return ActorRunReport(**base)


def _seed_source(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'x') RETURNING id"),
                {"n": name},
            ).scalar_one()
        )


def _seed_ingestion_run(source_id: int) -> str:
    run_id = str(uuid4())
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO ingestion_runs (id, user_id, source_id, trigger, status)
                VALUES (:id, 1, :sid, 'manual', 'running')
                """
            ),
            {"id": run_id, "sid": source_id},
        )
    return run_id


def _rows(source_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        rows = c.execute(
            text(
                """
                SELECT account, status, apify_run_id, cost_usd, charged_events,
                       items_scraped, items_kept, ingestion_run_id, started_at
                FROM apify_runs WHERE source_id = :sid ORDER BY id
                """
            ),
            {"sid": source_id},
        ).mappings()
        return [dict(r) for r in rows]


def test_record_inserts_rows_and_returns_quantized_total() -> None:
    sid = _seed_source("apify-obs-total")
    total = record_apify_runs(
        user_id=1,
        source_id=sid,
        ingestion_run_id=None,
        reports=[
            _report(),
            _report(account="spacex", apify_run_id="RUN2", cost_usd=0.0009, charged_events=None),
        ],
    )
    assert total == Decimal("0.003000")
    rows = _rows(sid)
    assert [r["account"] for r in rows] == ["nasa", "spacex"]
    assert rows[0]["cost_usd"] == Decimal("0.002100")
    assert rows[0]["charged_events"] == {"apify-default-dataset-item": 5}
    assert rows[0]["started_at"] is not None
    assert rows[1]["charged_events"] is None
    assert all(r["ingestion_run_id"] is None for r in rows)


def test_record_updates_ingestion_run_aggregate() -> None:
    sid = _seed_source("apify-obs-aggregate")
    run_id = _seed_ingestion_run(sid)
    record_apify_runs(user_id=1, source_id=sid, ingestion_run_id=run_id, reports=[_report()])
    with connection() as c:
        agg = c.execute(
            text("SELECT api_cost_usd FROM ingestion_runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert agg == Decimal("0.002100")
    assert str(_rows(sid)[0]["ingestion_run_id"]) == run_id


def test_record_without_cost_keeps_null_and_returns_none() -> None:
    """Apify a veces asienta el costo tarde: la fila queda con cost NULL, visible, sin agregado."""
    sid = _seed_source("apify-obs-nocost")
    run_id = _seed_ingestion_run(sid)
    total = record_apify_runs(
        user_id=1,
        source_id=sid,
        ingestion_run_id=run_id,
        reports=[_report(status="error", apify_run_id=None, cost_usd=None, charged_events=None)],
    )
    assert total is None
    [row] = _rows(sid)
    assert row["status"] == "error"
    assert row["cost_usd"] is None
    with connection() as c:
        agg = c.execute(
            text("SELECT api_cost_usd FROM ingestion_runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
    assert agg is None


def test_record_empty_list_is_noop() -> None:
    sid = _seed_source("apify-obs-empty")
    assert record_apify_runs(user_id=1, source_id=sid, ingestion_run_id=None, reports=[]) is None
    assert _rows(sid) == []
