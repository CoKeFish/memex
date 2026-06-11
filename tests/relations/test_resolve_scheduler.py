"""Job `graph_resolve` del scheduler: registrado y APAGADO por default; habilitable con su
intervalo; `LLMQuotaError` se captura (lo pagado ya se aplicó adentro) y el job no explota."""

from __future__ import annotations

import pytest

from memex.llm.client import LLMQuotaError
from memex.relations.resolve import ResolveStats
from memex.scheduler import jobs as jobs_mod
from memex.scheduler.config import SchedulerSettings, build_jobs


def test_graph_resolve_registrado() -> None:
    assert "graph_resolve" in jobs_mod.all_jobs()


def test_graph_resolve_apagado_por_default() -> None:
    assert build_jobs(SchedulerSettings(enabled_jobs="")) == []


def test_graph_resolve_se_habilita_con_intervalo() -> None:
    jobs = build_jobs(SchedulerSettings(enabled_jobs="graph_resolve"))
    assert [j.name for j in jobs] == ["graph_resolve"]
    assert jobs[0].default_interval == "P1D"
    jobs2 = build_jobs(
        SchedulerSettings(enabled_jobs="graph_resolve", interval_graph_resolve="PT6H")
    )
    assert jobs2[0].default_interval == "PT6H"


@pytest.mark.asyncio
async def test_run_graph_resolve_delega(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(user_id: int, **kw: object) -> ResolveStats:
        return ResolveStats(pairs=3, confirmed_recibo=2)

    monkeypatch.setattr(jobs_mod, "run_resolve", _fake)
    stats = await jobs_mod.run_graph_resolve(1)
    assert stats.pairs == 3 and stats.confirmed_recibo == 2


@pytest.mark.asyncio
async def test_run_graph_resolve_captura_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _quota(user_id: int, **kw: object) -> ResolveStats:
        raise LLMQuotaError(402, "sin saldo")

    monkeypatch.setattr(jobs_mod, "run_resolve", _quota)
    stats = await jobs_mod.run_graph_resolve(1)
    assert stats.errors == 1  # capturada: el daemon sigue; lo pagado ya se aplicó adentro
