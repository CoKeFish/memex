"""Merge/enriquecimiento LLM de la consolidación, con un LLMClient FALSO (sin red).

Cubre: enriquece un consolidado de varias copias (suma lugar/descripción que el ganador no tenía),
es idempotente (no re-llama si el grupo no cambió), ignora consolidados de una sola copia, y cae
al texto del ganador si la respuesta no parsea.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.merge_llm import (
    MergeMember,
    _member_signature,
    _parse_merged,
    run_merge,
)


class FakeLLM:
    name: ClassVar[str] = "fake"

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
            usage=LLMUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _seed_event(title: str, *, location: str = "", description: str = "") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events "
                    "(user_id, source_inbox_ids, title, starts_on, location, description) "
                    "VALUES (1, ARRAY[]::bigint[], :t, :d, :loc, :desc) RETURNING id"
                ),
                {"t": title, "d": date(2026, 6, 3), "loc": location, "desc": description},
            ).scalar_one()
        )


def _seed_cons(winner_id: int, title: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated "
                    "(user_id, title, starts_on, winner_event_id) "
                    "VALUES (1, :t, :d, :w) RETURNING id"
                ),
                {"t": title, "d": date(2026, 6, 3), "w": winner_id},
            ).scalar_one()
        )


def _link(cons_id: int, event_id: int) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
                "VALUES (1, :c, :e)"
            ),
            {"c": cons_id, "e": event_id},
        )


def _cons(cons_id: int) -> dict[str, object]:
    with connection() as c:
        row = c.execute(
            text(
                "SELECT title, location, description, merge_signature "
                "FROM mod_calendar_consolidated WHERE id = :id"
            ),
            {"id": cons_id},
        ).first()
    assert row is not None
    return {"title": row[0], "location": row[1], "description": row[2], "sig": row[3]}


# ----- puro ---------------------------------------------------------------------- #


def test_parse_merged_ok() -> None:
    fb = MergeMember("Base", "", "")
    m = _parse_merged('{"title": "X", "location": "Centro", "description": "nota"}', fb)
    assert (m.title, m.location, m.description) == ("X", "Centro", "nota")


def test_parse_merged_garbage_falls_back_to_winner() -> None:
    fb = MergeMember("Dentista", "Av 1", "traer estudios")
    m = _parse_merged("no json", fb)
    assert m == m.__class__("Dentista", "Av 1", "traer estudios")


def test_member_signature_is_stable_and_order_sensitive() -> None:
    a = MergeMember("T", "L", "D")
    b = MergeMember("T2", "", "")
    assert _member_signature([a, b]) == _member_signature([a, b])
    assert _member_signature([a, b]) != _member_signature([b, a])


# ----- DB ------------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_merge_enriches_multi_copy() -> None:
    w = _seed_event("Dentista", location="")  # ganador, sin lugar
    o = _seed_event("Cita Dentalink", location="Av Centro 123", description="traer radiografía")
    cons = _seed_cons(w, "Dentista")
    _link(cons, w)
    _link(cons, o)

    fake = FakeLLM(
        '{"title": "Dentista", "location": "Av Centro 123", "description": "traer radiografía"}'
    )
    stats = await run_merge(1, client=fake)

    assert stats.merged == 1
    row = _cons(cons)
    assert row["location"] == "Av Centro 123"  # sumó el lugar que el ganador no tenía
    assert row["description"] == "traer radiografía"
    assert row["sig"] is not None


@pytest.mark.asyncio
async def test_merge_idempotent_skips_unchanged() -> None:
    w = _seed_event("Dentista")
    o = _seed_event("Cita Dentalink", location="Av Centro 123")
    cons = _seed_cons(w, "Dentista")
    _link(cons, w)
    _link(cons, o)

    fake = FakeLLM('{"title": "Dentista", "location": "Av Centro 123", "description": ""}')
    await run_merge(1, client=fake)
    stats2 = await run_merge(1, client=fake)  # grupo sin cambios → estable

    assert stats2.skipped == 1
    assert fake.calls == 1  # no re-llamó al LLM


@pytest.mark.asyncio
async def test_merge_ignores_single_copy() -> None:
    w = _seed_event("Solo")
    cons = _seed_cons(w, "Solo")
    _link(cons, w)

    stats = await run_merge(
        1, client=FakeLLM('{"title": "Solo", "location": "", "description": ""}')
    )

    assert stats.consolidated == 0  # un solo crudo → nada que combinar


@pytest.mark.asyncio
async def test_merge_parse_fallback_keeps_winner_text() -> None:
    w = _seed_event("Dentista", location="Consultorio A")
    o = _seed_event("Otra copia", location="Otro lugar")
    cons = _seed_cons(w, "Dentista")
    _link(cons, w)
    _link(cons, o)

    stats = await run_merge(1, client=FakeLLM("respuesta no-json"))

    assert stats.merged == 1  # aplicó el fallback (texto del ganador)
    assert _cons(cons)["title"] == "Dentista"
