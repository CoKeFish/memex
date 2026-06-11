"""Job `graph` del scheduler: registrado y APAGADO por default; `run_graph_cycle` encadena
build→detect→validate (validador mockeado, sin LLM) y `LLMQuotaError` corta SOLO la validación."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.llm.client import LLMQuotaError
from memex.relations.clusters_llm import ClusterPartitionStats
from memex.scheduler import jobs as jobs_mod
from memex.scheduler.config import SchedulerSettings, build_jobs


def _registro(conn: Connection, activity: str, event_id: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_bienestar_registros (user_id, activity, occurred_at, event_id) "
                "VALUES (1, :a, NOW(), :e) RETURNING id"
            ),
            {"a": activity, "e": event_id},
        ).scalar_one()
    )


def _seed_triangle() -> None:
    """Tres hechos del MISMO evento → el build materializa el triángulo «mismo_evento» desde la
    fuente de verdad (antes se sembraban aristas `afiliado` directas sin respaldo en
    `person_orgs`; la reconciliación del build ahora las barrería, como corresponde)."""
    with connection() as c:
        for act in ("a", "b", "c"):
            _registro(c, act, "evt-triangulo")


def test_graph_job_registrado() -> None:
    assert "graph" in jobs_mod.all_jobs()


def test_graph_job_apagado_por_default() -> None:
    assert build_jobs(SchedulerSettings(enabled_jobs="")) == []


def test_graph_job_se_habilita() -> None:
    jobs = build_jobs(SchedulerSettings(enabled_jobs="graph"))
    assert [j.name for j in jobs] == ["graph"]
    assert jobs[0].default_interval == "P1D"


@pytest.mark.asyncio
async def test_run_graph_cycle_encadena(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_triangle()

    async def _fake(user_id: int, *, limit: int | None = None) -> ClusterPartitionStats:
        return ClusterPartitionStats(blobs=1, groups=1)

    monkeypatch.setattr(jobs_mod, "run_cluster_partition", _fake)
    cycle = await jobs_mod.run_graph_cycle(1)
    assert cycle.detected == 1
    assert cycle.new_candidates == 1
    assert cycle.confirmed == 1
    assert cycle.errors == 0


@pytest.mark.asyncio
async def test_run_graph_cycle_quota_corta_solo_validacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_triangle()

    async def _quota(user_id: int, *, limit: int | None = None) -> ClusterPartitionStats:
        raise LLMQuotaError(402, "sin saldo")

    monkeypatch.setattr(jobs_mod, "run_cluster_partition", _quota)
    cycle = await jobs_mod.run_graph_cycle(1)
    assert cycle.detected == 1  # lo determinista corrió igual
    assert "validate:no_quota" in cycle.steps_failed
    assert cycle.confirmed == 0
