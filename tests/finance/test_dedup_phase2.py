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


def _seed_tx(*, counterparty: str = "Rappi", inbox_ids: list[int] | None = None) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_transactions "
                    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                    " occurred_at_precision, counterparty) "
                    "VALUES (1, CAST(:ids AS BIGINT[]), 'egreso', 100, 'USD', :at, 'datetime', "
                    " :cp) "
                    "RETURNING id"
                ),
                {"at": _AT, "cp": counterparty, "ids": list(inbox_ids or [])},
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


def test_fmt_tx_shows_source_kind_when_known() -> None:
    """El juez ve entre corchetes el TIPO de fuente de cada lado (y nada si no se conoce)."""
    from memex.modules.finance.dedup_llm import _fmt_tx

    base = {
        "direction": "egreso",
        "amount": Decimal("13000"),
        "currency": "COP",
        "category": "otros",
        "counterparty": "Red de recaudo",
        "place": "",
        "occurred_at": _AT,
        "precision": "date",
        "description": "",
    }
    with_kind = _fmt_tx("A", PairTxView(**base, source_kind="documento aportado"))  # type: ignore[arg-type]
    assert with_kind.startswith("A [documento aportado]: egreso 13000 COP")
    without = _fmt_tx("B", PairTxView(**base))  # type: ignore[arg-type]
    assert without.startswith("B: egreso") and "[" not in without


def test_load_candidates_labels_source_kind_of_each_side() -> None:
    """`_load_candidates` resuelve la categoría de fuente por mensaje (documentos → 'documento
    aportado…') y marca 'registro del agente' a lo que no tiene mensaje de origen."""
    from uuid import uuid4

    from memex.modules.finance.dedup_llm import _load_candidates

    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'documentos') "
                "RETURNING id"
            ),
            {"n": f"doc-f2-{uuid4().hex[:6]}"},
        ).scalar_one()
        iid = int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, :at, CAST('{}' AS JSONB)) RETURNING id"
                ),
                {"s": sid, "e": f"doc-{sid}", "at": _AT},
            ).scalar_one()
        )
    a = _seed_tx(inbox_ids=[iid])
    b = _seed_tx()  # sin mensaje de origen → registro del agente
    _seed_candidate(a, b)
    try:
        with connection() as c:
            cands = _load_candidates(c, 1, 10_000)
        mine = next(x for x in cands if {x.a_id, x.b_id} == {a, b})
        views = {mine.a_id: mine.a, mine.b_id: mine.b}
        assert views[a].source_kind == "documento aportado (p. ej. extracto bancario)"
        assert views[b].source_kind == "registro del agente"
    finally:
        # no dejar un 'candidate' vivo para el user 1: otros tests cuentan llamadas al juez.
        with connection() as c:
            c.execute(
                text(
                    "DELETE FROM mod_finance_dedup_candidates "
                    "WHERE transaction_a_id = :lo AND transaction_b_id = :hi"
                ),
                {"lo": min(a, b), "hi": max(a, b)},
            )


def _pair_id(a: int, b: int) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "SELECT id FROM mod_finance_dedup_candidates "
                    "WHERE transaction_a_id = :lo AND transaction_b_id = :hi"
                ),
                {"lo": min(a, b), "hi": max(a, b)},
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_rejudge_by_pair_id_overrides_previous_llm_decision() -> None:
    """`pair_ids` re-juzga un par ya decidido por el juez (tras corregir el prompt) y la nueva
    decisión reemplaza a la anterior."""
    a, b = _seed_tx(), _seed_tx()
    _seed_candidate(a, b)
    await run_dedup_phase2(1, client=FakeLLM('{"same": false, "confidence": 0.8}'))
    assert _status(a, b) == ("rejected", "llm")

    fake = FakeLLM('{"same": true, "confidence": 0.95}')
    stats = await run_dedup_phase2(1, client=fake, pair_ids=[_pair_id(a, b)])
    assert (stats.pairs, stats.confirmed, fake.calls) == (1, 1, 1)
    assert _status(a, b) == ("confirmed", "llm")


@pytest.mark.asyncio
async def test_rejudge_skips_procedural_confirmations() -> None:
    """Lo auto-confirmado procedimentalmente es determinista, no opinión: no se re-juzga."""
    a, b = _seed_tx(), _seed_tx()
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_dedup_candidates "
                "(user_id, transaction_a_id, transaction_b_id, reason, score, status, decided_by) "
                "VALUES (1, :a, :b, 'amount+hora+contraparte+lugar', 1.0, 'confirmed', "
                "'procedural')"
            ),
            {"a": lo, "b": hi},
        )
    fake = FakeLLM('{"same": false, "confidence": 0.9}')
    stats = await run_dedup_phase2(1, client=fake, pair_ids=[_pair_id(a, b)])
    assert (stats.pairs, fake.calls) == (0, 0)
    assert _status(a, b) == ("confirmed", "procedural")
