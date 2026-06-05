"""Contrato v2: dedup por business-key vía `upsert_unique` (hackathones).

- hackathones: el mismo hackatón anunciado en dos mensajes colapsa a UNA fila con `source_inbox_ids`
  fusionados; re-extraer es idempotente.
- finance NO usa este mecanismo (su dedup es en dos fases + consolidación): ver `tests/finance/`.

(La disciplina «todo módulo declara `identity_fields`» la cubre mypy --strict: cada loader del
registry está tipado `-> InterestModule`, así que un módulo sin el campo no compila.)
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ExtractionItem, ModuleContext
from memex.modules.hackathones.module import HackathonModule
from memex.modules.hackathones.schema import HackathonItem


async def _persist(mod: Any, items: list[ExtractionItem]) -> int:
    with connection() as conn:
        ctx = ModuleContext(
            user_id=1,
            conn=conn,
            llm=cast(LLMClient, None),  # persist no usa el LLM (dedup determinista)
            deps={},
            summary_id=None,
            inbox_ids=(5, 6, 7),
        )
        return int(await mod.persist(ctx, items))


def _rows(table: str) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(text(f"SELECT * FROM {table} WHERE user_id=1 ORDER BY id"))
            .mappings()
            .all()
        ]


@pytest.mark.asyncio
async def test_hackathones_same_event_two_announcements_merges() -> None:
    h1 = HackathonItem(source_inbox_ids=(5,), name="HackBogota 2026", starts_on=date(2026, 7, 18))
    h2 = HackathonItem(source_inbox_ids=(6,), name="hackbogota 2026", starts_on=date(2026, 7, 18))
    await _persist(HackathonModule(), [h1])
    await _persist(HackathonModule(), [h2])
    rows = _rows("mod_hackathones_events")
    assert len(rows) == 1
    assert sorted(rows[0]["source_inbox_ids"]) == [5, 6]


@pytest.mark.asyncio
async def test_hackathones_re_extract_is_idempotent() -> None:
    h = HackathonItem(source_inbox_ids=(5,), name="NASA Space Apps", starts_on=date(2026, 10, 3))
    await _persist(HackathonModule(), [h])
    await _persist(HackathonModule(), [h])
    rows = _rows("mod_hackathones_events")
    assert len(rows) == 1 and sorted(rows[0]["source_inbox_ids"]) == [5]


@pytest.mark.asyncio
async def test_hackathones_distinct_events_not_merged() -> None:
    await _persist(
        HackathonModule(),
        [
            HackathonItem(source_inbox_ids=(5,), name="Hack A", starts_on=date(2026, 7, 1)),
            HackathonItem(source_inbox_ids=(6,), name="Hack B", starts_on=date(2026, 7, 1)),
        ],
    )
    assert len(_rows("mod_hackathones_events")) == 2
