"""Dedup FASE 2 (LLM) con un LLMClient FALSO (sin red).

Cubre: confirma cuando el LLM dice `same:true`, rechaza cuando dice `same:false`, sesgo a
coexistir ante respuesta no parseable, e idempotencia (solo toca `candidate`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time
from decimal import Decimal

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.dedup_llm import (
    PairEventView,
    _parse_decision,
    run_dedup_phase2,
)


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


def _seed_event(title: str, *, start_time: time | None = None) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events "
                    "(user_id, source_inbox_ids, title, starts_on, start_time) "
                    "VALUES (1, ARRAY[]::bigint[], :t, :d, :st) RETURNING id"
                ),
                {"t": title, "d": date(2026, 6, 3), "st": start_time},
            ).scalar_one()
        )


def _seed_candidate(a_id: int, b_id: int) -> None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, score) "
                "VALUES (1, :a, :b, 'time+title', 0.9)"
            ),
            {"a": lo, "b": hi},
        )


def _status(a_id: int, b_id: int) -> tuple[str, str | None]:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        row = c.execute(
            text(
                "SELECT status, decided_by FROM mod_calendar_dedup_candidates "
                "WHERE event_a_id = :a AND event_b_id = :b"
            ),
            {"a": lo, "b": hi},
        ).first()
    assert row is not None
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


# ----- _parse_decision (puro) ---------------------------------------------------- #


def test_parse_same_true() -> None:
    d = _parse_decision('{"same": true, "confidence": 0.9, "rationale": "mismo"}')
    assert d.same is True
    assert d.confidence == 0.9


def test_parse_garbage_biases_to_coexist() -> None:
    d = _parse_decision("no soy json")
    assert d.same is False
    assert d.rationale == "parse_fallback"


def test_parse_missing_same_biases_to_coexist() -> None:
    d = _parse_decision('{"confidence": 0.99}')
    assert d.same is False


def test_parse_clamps_confidence() -> None:
    assert _parse_decision('{"same": true, "confidence": 5}').confidence == 1.0


# ----- worker (DB + LLM falso) --------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirms_same_event() -> None:
    a = _seed_event("Dentista", start_time=time(10, 0))
    b = _seed_event("Cita Dentalink", start_time=time(10, 0))
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": true, "confidence": 0.92, "rationale": "misma cita"}')
    stats = await run_dedup_phase2(1, client=fake)

    assert (stats.pairs, stats.confirmed, stats.rejected) == (1, 1, 0)
    assert _status(a, b) == ("confirmed", "llm")


@pytest.mark.asyncio
async def test_rejects_distinct_events() -> None:
    a = _seed_event("Almuerzo con Ana", start_time=time(13, 0))
    b = _seed_event("Reunión de equipo", start_time=time(13, 0))
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": false, "confidence": 0.8, "rationale": "distintos"}')
    stats = await run_dedup_phase2(1, client=fake)

    assert (stats.confirmed, stats.rejected) == (0, 1)
    assert _status(a, b)[0] == "rejected"


@pytest.mark.asyncio
async def test_unparseable_response_rejects() -> None:
    a = _seed_event("X", start_time=time(9, 0))
    b = _seed_event("Y", start_time=time(9, 0))
    _seed_candidate(a, b)

    stats = await run_dedup_phase2(1, client=FakeLLM("la respuesta no es json"))

    assert stats.rejected == 1
    assert _status(a, b)[0] == "rejected"


@pytest.mark.asyncio
async def test_idempotent_only_processes_candidates() -> None:
    a = _seed_event("Dentista", start_time=time(10, 0))
    b = _seed_event("Cita Dentalink", start_time=time(10, 0))
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": true, "confidence": 0.9, "rationale": "x"}')
    await run_dedup_phase2(1, client=fake)
    stats2 = await run_dedup_phase2(1, client=fake)  # ya no hay 'candidate'

    assert stats2.pairs == 0
    assert fake.calls == 1  # no re-llamó al LLM


def test_disambiguate_pair_view_is_usable() -> None:
    # smoke del dataclass que viaja al LLM (sin red).
    v = PairEventView(
        title="Dentista",
        starts_on=date(2026, 6, 3),
        ends_on=None,
        start_time=time(10, 0),
        end_time=None,
        location="Centro",
        description="",
    )
    assert v.title == "Dentista"
