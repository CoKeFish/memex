"""run_calendar_cycle: orquestación pull→dedup→consolidate→merge→push (workers mockeados)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.consolidate import ConsolidationStats
from memex.modules.calendar.dedup_llm import DedupPhase2Stats
from memex.modules.calendar.merge_llm import MergeStats
from memex.modules.calendar.sync import PushStats, SyncStats
from memex.scheduler.jobs import run_calendar_cycle


@pytest.fixture
def accounts() -> dict[str, int]:
    """3 cuentas: wb=ON+enabled, ro=OFF+enabled, off=ON+deshabilitada."""
    with connection() as conn:
        rows = conn.execute(
            text(
                """
                INSERT INTO mod_calendar_provider_accounts
                  (user_id, provider, account_label, calendar_id, token_path_env,
                   enabled, write_back)
                VALUES
                  (1,'google','wb','cal-wb','TOK_WB',  TRUE,  TRUE),
                  (1,'google','ro','cal-ro','TOK_RO',  TRUE,  FALSE),
                  (1,'google','off','cal-off','TOK_OFF', FALSE, TRUE)
                RETURNING id, account_label
                """
            )
        ).all()
    return {str(label): int(id_) for id_, label in rows}


@pytest.mark.asyncio
async def test_cycle_orchestrates_all_steps(
    monkeypatch: pytest.MonkeyPatch, accounts: dict[str, int]
) -> None:
    pulled: list[int] = []
    pushed: list[int] = []
    counts = {"dedup": 0, "consolidate": 0, "merge": 0}

    async def fake_pull(uid: int, acc: int) -> SyncStats:
        pulled.append(acc)
        return SyncStats(pulled=2)

    async def fake_push(uid: int, acc: int) -> PushStats:
        pushed.append(acc)
        return PushStats(created=1)

    async def fake_dedup(uid: int) -> DedupPhase2Stats:
        counts["dedup"] += 1
        return DedupPhase2Stats()

    def fake_consolidate(uid: int) -> ConsolidationStats:
        counts["consolidate"] += 1
        return ConsolidationStats(consolidated=3)

    async def fake_merge(uid: int) -> MergeStats:
        counts["merge"] += 1
        return MergeStats()

    monkeypatch.setattr("memex.scheduler.jobs.run_pull", fake_pull)
    monkeypatch.setattr("memex.scheduler.jobs.run_push", fake_push)
    monkeypatch.setattr("memex.scheduler.jobs.run_dedup_phase2", fake_dedup)
    monkeypatch.setattr("memex.scheduler.jobs.run_consolidation", fake_consolidate)
    monkeypatch.setattr("memex.scheduler.jobs.run_merge", fake_merge)

    stats = await run_calendar_cycle(1)

    # pull en las 2 habilitadas, NO en la deshabilitada
    assert sorted(pulled) == sorted([accounts["wb"], accounts["ro"]])
    # dedup/consolidate/merge una sola vez (user-level)
    assert counts == {"dedup": 1, "consolidate": 1, "merge": 1}
    # push SOLO en la cuenta write_back habilitada
    assert pushed == [accounts["wb"]]

    assert stats.accounts == 2
    assert stats.pulled == 4  # 2 cuentas x 2
    assert stats.consolidated == 3
    assert stats.pushed == 1
    assert stats.errors == 0


@pytest.mark.asyncio
async def test_cycle_step_error_continues_to_push(
    monkeypatch: pytest.MonkeyPatch, accounts: dict[str, int]
) -> None:
    pushed: list[int] = []

    async def fake_pull(uid: int, acc: int) -> SyncStats:
        return SyncStats(pulled=1)

    async def fake_dedup(uid: int) -> DedupPhase2Stats:
        return DedupPhase2Stats()

    def fake_consolidate(uid: int) -> ConsolidationStats:
        return ConsolidationStats()

    async def boom_merge(uid: int) -> MergeStats:
        raise RuntimeError("merge fail")

    async def fake_push(uid: int, acc: int) -> PushStats:
        pushed.append(acc)
        return PushStats(created=1)

    monkeypatch.setattr("memex.scheduler.jobs.run_pull", fake_pull)
    monkeypatch.setattr("memex.scheduler.jobs.run_dedup_phase2", fake_dedup)
    monkeypatch.setattr("memex.scheduler.jobs.run_consolidation", fake_consolidate)
    monkeypatch.setattr("memex.scheduler.jobs.run_merge", boom_merge)
    monkeypatch.setattr("memex.scheduler.jobs.run_push", fake_push)

    stats = await run_calendar_cycle(1)

    # merge falló pero el push igual corrió (es I/O de proveedor, best-effort por paso)
    assert pushed == [accounts["wb"]]
    assert "merge" in stats.steps_failed
    assert stats.errors >= 1
