"""El gate del mantenimiento por lotes de identidades (`batch_maintenance_enabled`, default OFF).

Con el flag apagado, `run_identidades_cycle` salta `merge` (dedup phase-2) y `organize` (jerarquía)
— los reemplaza el resolvedor contextual por-correo. La sincronización de contactos no se gatea.
"""

from __future__ import annotations

import pytest

from memex.db import connection
from memex.modules.identidades.dedup_llm import MergePhase2Stats
from memex.modules.identidades.hierarchy import OrganizeStats
from memex.modules.identidades.settings import upsert_settings
from memex.scheduler import jobs


def _install_spies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Reemplaza merge/organize por espías que registran si se llamaron (sin LLM)."""
    calls: list[str] = []

    async def fake_merge(_uid: int) -> MergePhase2Stats:
        calls.append("merge")
        return MergePhase2Stats()

    async def fake_organize(_uid: int) -> OrganizeStats:
        calls.append("organize")
        return OrganizeStats()

    monkeypatch.setattr(jobs, "run_merge_phase2", fake_merge)
    monkeypatch.setattr(jobs, "run_organize", fake_organize)
    return calls


@pytest.mark.asyncio
async def test_batch_off_by_default_skips_merge_and_organize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_spies(monkeypatch)
    cycle = await jobs.run_identidades_cycle(1)  # sin fila de settings → batch OFF
    assert calls == []
    assert cycle.merged == 0
    assert cycle.linked == 0


@pytest.mark.asyncio
async def test_batch_on_runs_merge_and_organize(monkeypatch: pytest.MonkeyPatch) -> None:
    # El ciclo abre su PROPIA conexión → sembrar con un commit aparte (no con el fixture `conn`).
    with connection() as c:
        upsert_settings(c, 1, batch_maintenance_enabled=True)
    calls = _install_spies(monkeypatch)
    await jobs.run_identidades_cycle(1)
    assert calls == ["merge", "organize"]
