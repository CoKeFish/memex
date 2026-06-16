"""Slice 5: minería de reglas INTERCALADA entre ventanas del lote (procesamiento incremental).

Cubre el gating de `_mine_between_windows`: solo mina con el gate encendido, `relevance` en las
etapas del lote y `mining_interleave` ON. El comportamiento real (acumular no-relevantes → regla
auto-activa → la ventana siguiente corto-circuita) se valida en el smoke con LLM real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from memex.db import connection
from memex.processing import lots
from memex.processing.lots import ProcessingLot, _mine_between_windows
from memex.relevance.mining import MiningStats
from memex.relevance.settings import upsert_settings


def _lot(stages: list[str]) -> ProcessingLot:
    return ProcessingLot(
        user_id=1,
        stages=stages,
        config={},
        target_ids=[1, 2, 3],
        frontier=0,
        window_size=2,
        status="active",
        history=[],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _fake_mining(calls: list[int]) -> Callable[[int], Awaitable[MiningStats]]:
    async def _run(user_id: int) -> MiningStats:
        calls.append(user_id)
        return MiningStats(senders=1, proposed=1, activated=1)

    return _run


def test_interleave_skips_without_relevance_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(lots, "run_rule_mining", _fake_mining(calls))
    with connection() as c:
        upsert_settings(c, 1, enabled=True, mining_interleave=True)
    assert asyncio.run(_mine_between_windows(1, _lot(["extract"]))) is None
    assert calls == []  # el lote no juzga relevancia → no se mina


def test_interleave_skips_when_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(lots, "run_rule_mining", _fake_mining(calls))
    # gate apagado (default) → no-op aunque relevance esté en las etapas
    assert asyncio.run(_mine_between_windows(1, _lot(["relevance", "extract"]))) is None
    assert calls == []


def test_interleave_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(lots, "run_rule_mining", _fake_mining(calls))
    with connection() as c:
        upsert_settings(c, 1, enabled=True, mining_interleave=False)
    assert asyncio.run(_mine_between_windows(1, _lot(["relevance", "extract"]))) is None
    assert calls == []


def test_interleave_mines_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(lots, "run_rule_mining", _fake_mining(calls))
    with connection() as c:
        upsert_settings(c, 1, enabled=True, mining_interleave=True)
    out = asyncio.run(_mine_between_windows(1, _lot(["relevance", "extract"])))
    assert calls == [1]  # minó una vez
    assert out is not None
    assert (out["proposed"], out["activated"]) == (1, 1)
