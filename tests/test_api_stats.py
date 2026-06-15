"""Tests del router `stats` (observabilidad del pipeline: /stats/pipeline y /stats/overview).

Patrón de test_api_metrics/test_api_finance: fixture `client` (auth off), helpers de siembra con
`connection()`+`text()`, y scoping cross-tenant con `seed_user2`. Las tablas de observabilidad
(ingestion_runs, worker_runs, work_item_failures, mod_calendar_*) se limpian por cascada de FK
`user_id` cuando `_reset_tables` trunca `users` (conftest.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from memex.db import connection

_JOBS = ("classify", "extract", "ocr", "calendar", "log_purge")


def _seed_source(name: str, stype: str = "imap", user_id: int = 1) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, :t) RETURNING id"),
                {"u": user_id, "n": name, "t": stype},
            ).scalar_one()
        )


def _seed_run(
    source_id: int,
    user_id: int = 1,
    *,
    status: str = "ok",
    trigger: str = "cli",
    posted: int = 0,
    inserted: int = 0,
    duplicates: int = 0,
    errors: int = 0,
    filtered: int = 0,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> str:
    rid = uuid.uuid4()
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO ingestion_runs
                  (id, user_id, source_id, trigger, status, started_at, ended_at,
                   posted, inserted, duplicates, errors, filtered, error_class, error_message)
                VALUES
                  (:id, :uid, :src, :trig, :status, COALESCE(:started, NOW()), :ended,
                   :posted, :inserted, :duplicates, :errors, :filtered, :ec, :em)
                """
            ),
            {
                "id": rid,
                "uid": user_id,
                "src": source_id,
                "trig": trigger,
                "status": status,
                "started": started_at,
                "ended": ended_at,
                "posted": posted,
                "inserted": inserted,
                "duplicates": duplicates,
                "errors": errors,
                "filtered": filtered,
                "ec": error_class,
                "em": error_message,
            },
        )
    return str(rid)


def _seed_worker(
    job: str,
    user_id: int = 1,
    *,
    status: str = "ok",
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO worker_runs
                  (user_id, job, status, stats, error, started_at, finished_at)
                VALUES (:uid, :job, :status, CAST(:stats AS JSONB), :err,
                        COALESCE(:started, NOW()), :finished)
                """
            ),
            {
                "uid": user_id,
                "job": job,
                "status": status,
                "stats": json.dumps(stats or {}),
                "err": error,
                "started": started_at,
                "finished": finished_at,
            },
        )


def _seed_inbox(
    source_id: int,
    user_id: int = 1,
    *,
    external_id: str | None = None,
    processed_at: datetime | None = None,
    process_error: str | None = None,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO inbox
                      (user_id, source_id, external_id, occurred_at, payload,
                       processed_at, process_error)
                    VALUES (:uid, :src, :ext, NOW(), CAST('{}' AS JSONB), :proc, :err)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "src": source_id,
                    "ext": external_id or uuid.uuid4().hex,
                    "proc": processed_at,
                    "err": process_error,
                },
            ).scalar_one()
        )


def _seed_failure(
    inbox_id: int, user_id: int = 1, *, stage: str = "summarize", status: str = "review"
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO work_item_failures
                  (user_id, stage, inbox_id, attempts, last_error, status)
                VALUES (:uid, :stage, :iid, 3, 'boom', :status)
                """
            ),
            {"uid": user_id, "stage": stage, "iid": inbox_id, "status": status},
        )


def _seed_consolidated(user_id: int = 1, title: str = "ev") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
                    "VALUES (:uid, :title, :d) RETURNING id"
                ),
                {"uid": user_id, "title": title, "d": date(2026, 6, 1)},
            ).scalar_one()
        )


def _seed_conflict(user_id: int = 1, status: str = "pending") -> None:
    a = _seed_consolidated(user_id, "a")
    b = _seed_consolidated(user_id, "b")
    lo, hi = (a, b) if a < b else (b, a)
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_conflicts
                  (user_id, consolidated_a_id, consolidated_b_id, reason, status)
                VALUES (:uid, :a, :b, 'overlap', :status)
                """
            ),
            {"uid": user_id, "a": lo, "b": hi, "status": status},
        )


# --- /stats/pipeline -----------------------------------------------------------------------------


def test_pipeline_empty(client: Any) -> None:
    body = client.get("/stats/pipeline").json()
    assert body["sources"] == []
    # los jobs fijos aparecen aunque no haya corridas, con latest=None
    assert [w["job"] for w in body["workers"]] == list(_JOBS)
    assert all(w["latest"] is None and w["is_stale"] is False for w in body["workers"])
    assert body["ingestion"]["runs"] == []
    assert body["ingestion"]["totals"] == {
        "posted": 0,
        "inserted": 0,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
        "runs": 0,
        "unbalanced": 0,
        "api_cost_usd": 0.0,
    }


def test_pipeline_source_health(client: Any) -> None:
    sid = _seed_source("imap-1")
    old = datetime(2026, 5, 1, tzinfo=UTC)
    new = datetime(2026, 5, 2, tzinfo=UTC)
    _seed_run(sid, status="ok", posted=10, inserted=8, filtered=2, started_at=old)
    _seed_run(sid, status="failed", error_class="IMAPError", started_at=new, posted=0)
    s = client.get("/stats/pipeline").json()["sources"]
    assert len(s) == 1
    row = s[0]
    assert row["source_id"] == sid
    assert row["name"] == "imap-1"
    assert row["type"] == "imap"
    assert row["enabled"] is True
    assert row["success_rate"] == 0.5  # 1 ok de 2 terminadas
    assert row["total_inserted"] == 8
    assert row["total_filtered"] == 2
    # last_run = la más reciente (la fallida)
    assert row["last_run"]["status"] == "failed"
    assert row["last_run"]["error_class"] == "IMAPError"
    # sparkline viejo→nuevo
    assert [p["inserted"] for p in row["recent"]] == [8, 0]


def test_pipeline_source_no_runs(client: Any) -> None:
    _seed_source("sin-corridas")
    row = client.get("/stats/pipeline").json()["sources"][0]
    assert row["last_run"] is None
    assert row["success_rate"] == 0.0
    assert row["total_inserted"] == 0
    assert row["recent"] == []


def test_pipeline_ingestion_invariant_and_totals(client: Any) -> None:
    sid = _seed_source("s")
    # balanceada: 10 = 6 + 2 + 1 + 1
    _seed_run(sid, posted=10, inserted=6, duplicates=2, errors=1, filtered=1)
    # desbalanceada: 5 != 3 + 0 + 0 + 0
    _seed_run(sid, posted=5, inserted=3)
    ing = client.get("/stats/pipeline").json()["ingestion"]
    by_balanced = {r["balanced"]: r for r in ing["runs"]}
    assert by_balanced[True]["expected"] == 10
    assert by_balanced[False]["expected"] == 3
    assert all(r["source_name"] == "s" for r in ing["runs"])  # JOIN a sources
    assert ing["totals"]["posted"] == 15
    assert ing["totals"]["inserted"] == 9
    assert ing["totals"]["runs"] == 2
    assert ing["totals"]["unbalanced"] == 1


def test_pipeline_ingestion_window(client: Any) -> None:
    sid = _seed_source("s")
    _seed_run(sid, started_at=datetime(2026, 3, 1, tzinfo=UTC), posted=1, inserted=1)
    _seed_run(sid, started_at=datetime(2026, 4, 1, tzinfo=UTC), posted=1, inserted=1)
    _seed_run(sid, started_at=datetime(2026, 5, 1, tzinfo=UTC), posted=1, inserted=1)
    body = client.get("/stats/pipeline?since=2026-03-15&until=2026-04-15").json()
    assert len(body["ingestion"]["runs"]) == 1


def test_pipeline_workers_latest_and_stale(client: Any) -> None:
    # dos corridas del mismo job: la última (ok) gana
    _seed_worker("classify", status="error", started_at=datetime(2026, 5, 1, tzinfo=UTC))
    _seed_worker(
        "classify",
        status="ok",
        stats={"classified": 12},
        started_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    # worker colgado: running hace 1h
    _seed_worker("summarize", status="running", started_at=datetime.now(UTC) - timedelta(hours=1))
    # worker running reciente: NO stale
    _seed_worker("extract", status="running", started_at=datetime.now(UTC))
    workers = {w["job"]: w for w in client.get("/stats/pipeline").json()["workers"]}
    assert workers["classify"]["latest"]["status"] == "ok"
    assert workers["classify"]["latest"]["stats"] == {"classified": 12}
    assert workers["classify"]["is_stale"] is False
    assert workers["summarize"]["is_stale"] is True
    assert workers["extract"]["is_stale"] is False


def test_pipeline_unknown_job_appended(client: Any) -> None:
    _seed_worker("backfill", status="ok")  # job fuera de la lista fija
    jobs = [w["job"] for w in client.get("/stats/pipeline").json()["workers"]]
    assert jobs[: len(_JOBS)] == list(_JOBS)
    assert "backfill" in jobs[len(_JOBS) :]


def test_pipeline_cross_tenant(client: Any, seed_user2: int) -> None:
    mine = _seed_source("mine", user_id=1)
    other = _seed_source("theirs", user_id=seed_user2)
    _seed_run(mine, user_id=1, posted=1, inserted=1)
    _seed_run(other, user_id=seed_user2, posted=9, inserted=9)
    body = client.get("/stats/pipeline").json()
    assert [s["name"] for s in body["sources"]] == ["mine"]
    assert body["ingestion"]["totals"]["inserted"] == 1


# --- /stats/overview -----------------------------------------------------------------------------


def test_overview_empty(client: Any) -> None:
    body = client.get("/stats/overview").json()
    assert body == {
        "review": {"dead_letter": 0, "calendar_conflicts": 0, "total": 0},
        "inbox_pending": 0,
        "inbox_errors": 0,
        "stale_workers": 0,
    }


def test_overview_counts(client: Any) -> None:
    sid = _seed_source("s")
    # inbox: 1 pendiente, 1 con error, 1 procesado ok
    _seed_inbox(sid, external_id="pending")
    _seed_inbox(sid, external_id="err", process_error="boom")
    _seed_inbox(sid, external_id="done", processed_at=datetime.now(UTC))
    # dead-letter: 1 en review (sobre un inbox real), 1 'failing' que NO cuenta. Estos inbox ya
    # pasaron el procesamiento de inbox (su fallo es en summarize/extract) → no "pending".
    dl = _seed_inbox(sid, external_id="dl", processed_at=datetime.now(UTC))
    _seed_failure(dl, stage="summarize", status="review")
    failing = _seed_inbox(sid, external_id="failing", processed_at=datetime.now(UTC))
    _seed_failure(failing, stage="extract", status="failing")
    # conflicto de calendar pendiente (+ uno resuelto que NO cuenta)
    _seed_conflict(status="pending")
    _seed_conflict(status="resolved")
    # worker colgado
    _seed_worker("calendar", status="running", started_at=datetime.now(UTC) - timedelta(hours=2))

    body = client.get("/stats/overview").json()
    assert body["review"] == {"dead_letter": 1, "calendar_conflicts": 1, "total": 2}
    assert body["inbox_pending"] == 1
    assert body["inbox_errors"] == 1
    assert body["stale_workers"] == 1


def test_overview_cross_tenant(client: Any, seed_user2: int) -> None:
    s2 = _seed_source("theirs", user_id=seed_user2)
    _seed_inbox(s2, user_id=seed_user2, external_id="theirs-pending")
    body = client.get("/stats/overview").json()
    assert body["inbox_pending"] == 0


# --- /stats/alerts (OBS-4: alertas REALES, no mock) ----------------------------------------------


def test_alerts_empty(client: Any) -> None:
    assert client.get("/stats/alerts").json() == []


def test_alerts_failed_source(client: Any) -> None:
    sid = _seed_source("imap-x")
    _seed_run(
        sid,
        status="failed",
        error_message="AUTH falló",
        started_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    items = client.get("/stats/alerts").json()
    assert len(items) == 1
    a = items[0]
    assert a["kind"] == "run-failed"
    assert a["severity"] == "alta"
    assert "imap-x" in a["title"]
    assert a["detail"] == "AUTH falló"
    assert a["deep_link"] == "/pipeline"


def test_alerts_recovered_source_no_alert(client: Any) -> None:
    """Si la ÚLTIMA corrida fue ok, la fuente NO alerta (aunque antes haya fallado)."""
    sid = _seed_source("s")
    _seed_run(sid, status="failed", started_at=datetime(2026, 6, 1, tzinfo=UTC))
    _seed_run(sid, status="ok", started_at=datetime(2026, 6, 2, tzinfo=UTC))
    assert client.get("/stats/alerts").json() == []


def test_alerts_stale_worker(client: Any) -> None:
    _seed_worker("summarize", status="running", started_at=datetime.now(UTC) - timedelta(hours=1))
    items = client.get("/stats/alerts").json()
    assert [a["kind"] for a in items] == ["worker-stale"]
    assert "summarize" in items[0]["title"]


def test_alerts_worker_error_vs_saldo(client: Any) -> None:
    """Worker en error → 'run-failed'; si el error delata saldo/cuota → 'saldo' (crítica)."""
    when = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_worker("extract", status="error", error="boom genérico", started_at=when)
    _seed_worker(
        "summarize", status="error", error="DeepSeek 402 insufficient balance", started_at=when
    )
    by_id = {a["id"]: a for a in client.get("/stats/alerts").json()}
    assert by_id["worker-extract"]["kind"] == "run-failed"
    assert by_id["worker-extract"]["severity"] == "alta"
    assert by_id["worker-summarize"]["kind"] == "saldo"
    assert by_id["worker-summarize"]["severity"] == "critica"


def test_alerts_review_backlog(client: Any) -> None:
    sid = _seed_source("s")
    iid = _seed_inbox(sid, processed_at=datetime.now(UTC))
    _seed_failure(iid, status="review")
    _seed_conflict(status="pending")
    review = [a for a in client.get("/stats/alerts").json() if a["kind"] == "review"]
    assert len(review) == 1
    assert "2" in review[0]["title"]  # 1 dead-letter + 1 conflicto
    assert review[0]["severity"] == "info"
    assert review[0]["deep_link"] == "/revision"


def test_alerts_sorted_most_severe_first(client: Any) -> None:
    sid = _seed_source("s")
    _seed_run(sid, status="failed", started_at=datetime(2026, 6, 1, tzinfo=UTC))  # alta
    _seed_worker(
        "summarize",
        status="error",
        error="402 insufficient",
        started_at=datetime(2026, 6, 1, tzinfo=UTC),
    )  # critica
    iid = _seed_inbox(sid, processed_at=datetime.now(UTC))
    _seed_failure(iid, status="review")  # info
    sevs = [a["severity"] for a in client.get("/stats/alerts").json()]
    assert sevs[0] == "critica"
    assert sevs[-1] == "info"


def test_alerts_cross_tenant(client: Any, seed_user2: int) -> None:
    s2 = _seed_source("theirs", user_id=seed_user2)
    _seed_run(s2, user_id=seed_user2, status="failed", started_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert client.get("/stats/alerts").json() == []
