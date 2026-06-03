"""API + DB de la ingesta agendada (router /ingest, 0025): scheduler, schedule por fuente, runs.

Incluye el gating de `_desired_sources` del daemon contra la DB real (master toggle + enabled +
fetch_schedule), que es la query que decide qué fuentes se agendan.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.core.observability import ingestion_run
from memex.db import connection
from memex.ingest_scheduler.daemon import IngestScheduler
from memex.ingestors.runner import RunStats


def _set_daemon(enabled: bool, user_id: int = 1) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO ingest_scheduler_settings (user_id, daemon_enabled)
                VALUES (:uid, :de)
                ON CONFLICT (user_id) DO UPDATE SET daemon_enabled = :de, updated_at = NOW()
                """
            ),
            {"uid": user_id, "de": enabled},
        )


# ---- GET/PATCH /ingest/scheduler ---


def test_get_scheduler_default_off(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.get("/ingest/scheduler")
    assert r.status_code == 200
    body = r.json()
    assert body["daemon_enabled"] is False
    assert len(body["sources"]) == 1
    s = body["sources"][0]
    assert s["source_id"] == seed_source["id"]
    assert s["enabled"] is True
    assert isinstance(s["config"], dict)  # el front lo usa para el icono/etiqueta (sourceMeta)
    assert s["fetch_schedule"] is None
    assert s["latest"] is None


def test_patch_scheduler_toggles_daemon(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.patch("/ingest/scheduler", json={"daemon_enabled": True})
    assert r.status_code == 200
    assert r.json()["daemon_enabled"] is True
    # Persistió: un GET nuevo lo refleja.
    assert client.get("/ingest/scheduler").json()["daemon_enabled"] is True


# ---- PATCH /sources/{id} fetch_schedule ---


def test_patch_source_sets_and_clears_schedule(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    r = client.patch(f"/sources/{sid}", json={"fetch_schedule": "PT1H"})
    assert r.status_code == 200
    assert r.json()["fetch_schedule"] == "PT1H"
    # Aparece en el panel de scheduler.
    sched = client.get("/ingest/scheduler").json()
    assert sched["sources"][0]["fetch_schedule"] == "PT1H"
    # null lo limpia.
    r2 = client.patch(f"/sources/{sid}", json={"fetch_schedule": None})
    assert r2.status_code == 200
    assert r2.json()["fetch_schedule"] is None


def test_patch_source_invalid_schedule_422(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.patch(f"/sources/{seed_source['id']}", json={"fetch_schedule": "nope"})
    assert r.status_code == 422


def test_patch_source_enabled_untouched_by_schedule(
    client: Any, seed_source: dict[str, Any]
) -> None:
    # Setear fetch_schedule no debe togglear enabled (campos independientes).
    sid = seed_source["id"]
    client.patch(f"/sources/{sid}", json={"fetch_schedule": "P1D"})
    assert client.get("/ingest/scheduler").json()["sources"][0]["enabled"] is True


# ---- GET /ingest/runs ---


def _record_run(source_id: int, trigger: str, user_id: int = 1) -> None:
    with ingestion_run(user_id=user_id, source_id=source_id, trigger=trigger) as run:
        run.finalize(RunStats(posted=3, inserted=2, duplicates=1, errors=0, filtered=0))


def test_runs_empty_then_lists_with_origin(client: Any, seed_source: dict[str, Any]) -> None:
    assert client.get("/ingest/runs").json()["items"] == []

    _record_run(seed_source["id"], "daemon")
    items = client.get("/ingest/runs").json()["items"]
    assert len(items) == 1
    run = items[0]
    assert run["trigger"] == "daemon"
    assert run["status"] == "ok"
    assert run["source_id"] == seed_source["id"]
    assert run["posted"] == 3
    assert run["inserted"] == 2
    assert run["is_stale"] is False
    # `id` es un UUID serializado como string (clave del deep-link a /logs?run_id=).
    assert isinstance(run["id"], str) and len(run["id"]) >= 32


def test_runs_filter_by_origin(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _record_run(sid, "daemon")
    _record_run(sid, "manual")
    assert len(client.get("/ingest/runs?trigger=daemon").json()["items"]) == 1
    assert len(client.get("/ingest/runs?trigger=manual").json()["items"]) == 1
    assert client.get("/ingest/runs?trigger=backfill").json()["items"] == []
    assert len(client.get("/ingest/runs").json()["items"]) == 2


# ---- Daemon: _desired_sources contra la DB real ---


def _desired_ids(user_id: int = 1) -> list[int]:
    sched = IngestScheduler(user_id=user_id, sources=[])
    desired = sched._desired_sources()
    assert desired is not None
    return [s.source_id for s in desired]


def test_desired_sources_off_without_row(seed_source: dict[str, Any]) -> None:
    # Sin fila en ingest_scheduler_settings → apagado (lista vacía), aunque la fuente exista.
    assert _desired_ids() == []


def test_desired_sources_gating(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _set_daemon(True)
    # Daemon armado pero la fuente NO tiene schedule → no se agenda.
    assert _desired_ids() == []

    # Con schedule → se agenda.
    with connection() as c:
        c.execute(text("UPDATE sources SET fetch_schedule = 'PT1H' WHERE id = :sid"), {"sid": sid})
    assert _desired_ids() == [sid]

    # Fuente deshabilitada → excluida aunque tenga schedule.
    with connection() as c:
        c.execute(text("UPDATE sources SET enabled = FALSE WHERE id = :sid"), {"sid": sid})
    assert _desired_ids() == []


def test_desired_sources_off_master_excludes_scheduled(seed_source: dict[str, Any]) -> None:
    with connection() as c:
        c.execute(
            text("UPDATE sources SET fetch_schedule = 'PT1H' WHERE id = :sid"),
            {"sid": seed_source["id"]},
        )
    _set_daemon(False)
    assert _desired_ids() == []  # master apagado → nada se agenda
