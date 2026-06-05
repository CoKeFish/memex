"""Dedup FASE 2 (LLM) de finance con un LLMClient FALSO (sin red).

Cubre: confirma con `same:true`, rechaza con `same:false`, sesgo a coexistir ante respuesta no
parseable, e idempotencia (solo toca `candidate`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.finance.dedup_llm import PairTxView, _parse_decision, run_dedup_phase2

_AT = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


class FakeLLM:
    """Devuelve siempre el mismo `content`; cuenta llamadas. Cumple el Protocol LLMClient."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(
            content=self.content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _seed_tx(*, counterparty: str = "Rappi") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_transactions "
                    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                    " occurred_at_precision, counterparty) "
                    "VALUES (1, ARRAY[]::bigint[], 'egreso', 100, 'USD', :at, 'datetime', :cp) "
                    "RETURNING id"
                ),
                {"at": _AT, "cp": counterparty},
            ).scalar_one()
        )


def _seed_candidate(a_id: int, b_id: int) -> None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_dedup_candidates "
                "(user_id, transaction_a_id, transaction_b_id, reason, score) "
                "VALUES (1, :a, :b, 'amount+hora+contraparte', 0.8)"
            ),
            {"a": lo, "b": hi},
        )


def _status(a_id: int, b_id: int) -> tuple[str, str | None]:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        row = c.execute(
            text(
                "SELECT status, decided_by FROM mod_finance_dedup_candidates "
                "WHERE transaction_a_id = :a AND transaction_b_id = :b"
            ),
            {"a": lo, "b": hi},
        ).first()
    assert row is not None
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


# ----- _parse_decision (puro) ---------------------------------------------------- #


def test_parse_same_true() -> None:
    d = _parse_decision('{"same": true, "confidence": 0.9, "rationale": "mismo cargo"}')
    assert d.same is True
    assert d.confidence == 0.9


def test_parse_garbage_biases_to_coexist() -> None:
    d = _parse_decision("no soy json")
    assert d.same is False
    assert d.rationale == "parse_fallback"


# ----- worker (DB + LLM falso) --------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirms_same_charge() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_candidate(a, b)
    stats = await run_dedup_phase2(1, client=FakeLLM('{"same": true, "confidence": 0.9}'))
    assert (stats.pairs, stats.confirmed, stats.rejected) == (1, 1, 0)
    assert _status(a, b) == ("confirmed", "llm")


@pytest.mark.asyncio
async def test_rejects_distinct_charges() -> None:
    a, b = _seed_tx(counterparty="Rappi"), _seed_tx(counterparty="Cabify")
    _seed_candidate(a, b)
    stats = await run_dedup_phase2(1, client=FakeLLM('{"same": false, "confidence": 0.8}'))
    assert (stats.confirmed, stats.rejected) == (0, 1)
    assert _status(a, b)[0] == "rejected"


@pytest.mark.asyncio
async def test_unparseable_response_rejects() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_candidate(a, b)
    stats = await run_dedup_phase2(1, client=FakeLLM("la respuesta no es json"))
    assert stats.rejected == 1
    assert _status(a, b)[0] == "rejected"


@pytest.mark.asyncio
async def test_idempotent_only_processes_candidates() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_candidate(a, b)
    fake = FakeLLM('{"same": true, "confidence": 0.9}')
    await run_dedup_phase2(1, client=fake)
    stats2 = await run_dedup_phase2(1, client=fake)  # ya no hay 'candidate'
    assert stats2.pairs == 0
    assert fake.calls == 1  # no re-llamó al LLM


def test_pair_view_is_usable() -> None:
    v = PairTxView(
        direction="egreso",
        amount=Decimal("100"),
        currency="USD",
        category="comida",
        counterparty="Rappi",
        place="",
        occurred_at=_AT,
        precision="datetime",
        description="",
    )
    assert v.counterparty == "Rappi"
