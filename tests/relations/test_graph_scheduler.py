"""Job `graph` del scheduler: registrado y APAGADO por default; `run_graph_cycle` encadena
build→detect→validate (validador mockeado, sin LLM) y `LLMQuotaError` corta SOLO la validación."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.llm.client import LLMQuotaError
from memex.relations.clusters_llm import ClusterValidationStats
from memex.relations.edges import PRODUCER_IDENTIDADES, STATUS_CONFIRMED, Ref, propose_edge
from memex.scheduler import jobs as jobs_mod
from memex.scheduler.config import SchedulerSettings, build_jobs


def _person(conn: Connection, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, 'persona', :n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def _seed_triangle() -> None:
    with connection() as c:
        a, b, d = _person(c, "A"), _person(c, "B"), _person(c, "C")
        for x, y in ((a, b), (b, d), (a, d)):
            propose_edge(
                c,
                1,
                Ref("identidades:person", x),
                Ref("identidades:person", y),
                producer=PRODUCER_IDENTIDADES,
                relation_type="afiliado",
                status=STATUS_CONFIRMED,
            )


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

    async def _fake(user_id: int, *, limit: int | None = None) -> ClusterValidationStats:
        return ClusterValidationStats(clusters=1, confirmed=1)

    monkeypatch.setattr(jobs_mod, "run_cluster_validation", _fake)
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

    async def _quota(user_id: int, *, limit: int | None = None) -> ClusterValidationStats:
        raise LLMQuotaError(402, "sin saldo")

    monkeypatch.setattr(jobs_mod, "run_cluster_validation", _quota)
    cycle = await jobs_mod.run_graph_cycle(1)
    assert cycle.detected == 1  # lo determinista corrió igual
    assert "validate:no_quota" in cycle.steps_failed
    assert cycle.confirmed == 0
