"""Contrato v2: cada módulo produce VÉRTICES ÚNICOS (dedup de su unidad).

- finance/hackathones: dedup por business-key vía `upsert_unique` — el mismo pago/hackatón en dos
  mensajes (recibo + alerta del banco) colapsa a UNA fila con `source_inbox_ids` fusionados;
  re-extraer es idempotente. Cierra el fallo ER (entidades duplicadas) del barrido adversarial.

(La disciplina «todo módulo declara `identity_fields`» la cubre mypy --strict: cada loader del
registry está tipado `-> InterestModule`, así que un módulo sin el campo no compila.)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ExtractionItem, ModuleContext
from memex.modules.finance.module import FinanceModule
from memex.modules.finance.schema import ExpenseItem
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


def _exp(amount: str, ids: tuple[int, ...], *, merchant: str, occurred: date | None) -> ExpenseItem:
    return ExpenseItem(
        source_inbox_ids=ids,
        amount=Decimal(amount),
        currency="COP",
        category="comida",
        merchant=merchant,
        occurred_on=occurred,
        description="",
        evidence="",
    )


@pytest.mark.asyncio
async def test_finance_same_payment_two_messages_merges() -> None:
    # recibo del comercio + alerta del banco = el MISMO pago (comercio con otra grafía) → 1 vértice
    d = date(2026, 6, 15)
    await _persist(FinanceModule(), [_exp("48900", (5,), merchant="Rappi", occurred=d)])
    await _persist(FinanceModule(), [_exp("48900", (6,), merchant="RAPPI ", occurred=d)])
    rows = _rows("mod_finance_expenses")
    assert len(rows) == 1
    assert sorted(rows[0]["source_inbox_ids"]) == [5, 6]


@pytest.mark.asyncio
async def test_finance_re_extract_is_idempotent() -> None:
    e = _exp("12000", (5,), merchant="Juan Valdez", occurred=date(2026, 6, 1))
    await _persist(FinanceModule(), [e])
    await _persist(FinanceModule(), [e])
    rows = _rows("mod_finance_expenses")
    assert len(rows) == 1 and sorted(rows[0]["source_inbox_ids"]) == [5]


@pytest.mark.asyncio
async def test_finance_distinct_expenses_not_merged() -> None:
    # distinto monto → vértices distintos (no se sobre-deduplica)
    await _persist(
        FinanceModule(),
        [
            _exp("10000", (5,), merchant="Tienda", occurred=date(2026, 6, 1)),
            _exp("20000", (6,), merchant="Tienda", occurred=date(2026, 6, 1)),
        ],
    )
    assert len(_rows("mod_finance_expenses")) == 2


@pytest.mark.asyncio
async def test_finance_dedup_within_one_batch() -> None:
    # dos menciones del mismo pago en el MISMO lote también colapsan
    await _persist(
        FinanceModule(),
        [
            _exp("5000", (5,), merchant="Café", occurred=date(2026, 6, 2)),
            _exp("5000", (6,), merchant="café", occurred=date(2026, 6, 2)),
        ],
    )
    rows = _rows("mod_finance_expenses")
    assert len(rows) == 1 and sorted(rows[0]["source_inbox_ids"]) == [5, 6]


@pytest.mark.asyncio
async def test_hackathones_same_event_two_announcements_merges() -> None:
    h1 = HackathonItem(source_inbox_ids=(5,), name="HackBogota 2026", starts_on=date(2026, 7, 18))
    h2 = HackathonItem(source_inbox_ids=(6,), name="hackbogota 2026", starts_on=date(2026, 7, 18))
    await _persist(HackathonModule(), [h1])
    await _persist(HackathonModule(), [h2])
    rows = _rows("mod_hackathones_events")
    assert len(rows) == 1
    assert sorted(rows[0]["source_inbox_ids"]) == [5, 6]
