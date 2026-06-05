"""Tests del módulo calendar: schema, registry, Protocol, dedup determinista y dominio.

Los del schema/registry/dedup son puros; `events_in_range` y `contribute` tocan la DB de test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import SourceKind
from memex.core.trace import create_root, open_module_tracer
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, ResponseFormat
from memex.modules import known_modules, resolve
from memex.modules.calendar.dedup import DedupRow, mark_duplicates
from memex.modules.calendar.domain import (
    CalendarDomain,
    CalendarDomainReader,
    CalendarEvent,
    ContributedEvent,
)
from memex.modules.calendar.module import CalendarModule
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.contract import (
    CAP_EXTRACT,
    CAP_PROVIDE_DOMAIN,
    ExtractionItem,
    InterestModule,
    ModuleContext,
)

# ----- schema -------------------------------------------------------------------- #


def test_calendar_event_valid_with_coercion() -> None:
    e = CalendarEventItem(
        source_inbox_ids=[3],  # list → tuple
        title="Examen de Análisis",
        starts_on="2026-06-03",  # str → date
        start_time="15:30",  # str → time
        location="Aula 7",
        evidence="el examen es el 3/6 a las 15:30 en el aula 7",
    )
    assert e.starts_on == date(2026, 6, 3)
    assert e.start_time == time(15, 30)
    assert e.source_inbox_ids == (3,)
    assert e.ends_on is None
    assert e.end_time is None
    assert e.description == ""


def test_calendar_event_starts_on_required() -> None:
    with pytest.raises(ValidationError):
        CalendarEventItem(source_inbox_ids=(1,), title="x")  # type: ignore[call-arg]  # falta starts_on


def test_calendar_event_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        CalendarEventItem(
            source_inbox_ids=(1,),
            title="x",
            starts_on=date(2026, 6, 3),
            categoria="evento",  # type: ignore[call-arg]  # extra prohibido
        )


def test_calendar_event_is_extraction_item() -> None:
    assert issubclass(CalendarEventItem, ExtractionItem)


# ----- registry / Protocol ------------------------------------------------------- #


def test_known_modules_includes_calendar() -> None:
    assert "calendar" in known_modules()


def test_resolve_calendar_builds_module() -> None:
    module = resolve("calendar")()
    assert isinstance(module, CalendarModule)


def test_calendar_satisfies_interest_module() -> None:
    assert isinstance(CalendarModule(), InterestModule)


def test_calendar_capabilities_and_kinds() -> None:
    assert CAP_EXTRACT in CalendarModule.capabilities
    assert CAP_PROVIDE_DOMAIN in CalendarModule.capabilities  # lo nuevo vs finance
    assert CalendarModule.consumes_kinds == frozenset({SourceKind.EMAIL, SourceKind.CHAT})
    assert CalendarModule.depends_on == ()


# ----- dedup determinista (puro) ------------------------------------------------- #


def _row(
    event_id: int,
    title: str,
    *,
    starts_on: date = date(2026, 6, 3),
    ends_on: date | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    location: str = "",
) -> DedupRow:
    return DedupRow(
        event_id=event_id,
        title=title,
        location=location,
        starts_on=starts_on,
        ends_on=ends_on,
        start_time=start_time,
        end_time=end_time,
    )


def test_dedup_same_time_similar_title_marks_pair() -> None:
    a = _row(1, "Reunión de equipo", start_time=time(10, 0))
    b = _row(2, "Reunion de equipo", start_time=time(10, 0))  # sin tilde
    pairs = mark_duplicates([a, b], [])
    assert len(pairs) == 1
    assert (pairs[0].a_id, pairs[0].b_id) == (1, 2)
    assert pairs[0].reason == "time+title"


def test_dedup_same_time_different_text_no_pair() -> None:
    """Mismo horario NO alcanza: sin similitud de título ni lugar, no es candidato."""
    a = _row(1, "Dentista", start_time=time(10, 0), location="Centro")
    b = _row(2, "Cena con Ana", start_time=time(10, 0), location="Palermo")
    assert mark_duplicates([a, b], []) == []


def test_dedup_same_time_similar_location_marks_pair() -> None:
    a = _row(1, "Charla A", start_time=time(18, 0), location="Auditorio Central")
    b = _row(2, "Evento B", start_time=time(18, 0), location="Auditorio Central")
    pairs = mark_duplicates([a, b], [])
    assert len(pairs) == 1
    assert pairs[0].reason == "time+location"


def test_dedup_disjoint_times_same_title_no_pair() -> None:
    a = _row(1, "Clase de Cálculo", start_time=time(9, 0))
    b = _row(2, "Clase de Cálculo", start_time=time(18, 0))
    assert mark_duplicates([a, b], []) == []


def test_dedup_within_tolerance_marks_pair() -> None:
    # Gap 18:40 → 19:00 = 20 min, dentro de la tolerancia de 30 min.
    a = _row(1, "Llamada con cliente", start_time=time(18, 0), end_time=time(18, 40))
    b = _row(2, "Llamada con cliente", start_time=time(19, 0))
    pairs = mark_duplicates([a, b], [])
    assert len(pairs) == 1
    assert pairs[0].reason == "time+title"


def test_dedup_all_day_same_day_similar_title_marks_pair() -> None:
    a = _row(1, "Feriado nacional")  # sin hora → todo el día
    b = _row(2, "Feriado nacional")
    pairs = mark_duplicates([a, b], [])
    assert len(pairs) == 1


def test_dedup_all_day_different_day_no_pair() -> None:
    a = _row(1, "Feriado nacional", starts_on=date(2026, 6, 3))
    b = _row(2, "Feriado nacional", starts_on=date(2026, 6, 20))
    assert mark_duplicates([a, b], []) == []


def test_dedup_new_vs_existing_marks_pair() -> None:
    new = _row(10, "Vuelo a Córdoba", start_time=time(8, 0))
    existing = _row(3, "Vuelo a Cordoba", start_time=time(8, 0))
    pairs = mark_duplicates([new], [existing])
    assert len(pairs) == 1
    assert (pairs[0].a_id, pairs[0].b_id) == (3, 10)  # canónico a<b


def test_dedup_not_transitive() -> None:
    """A~B y B~C pero A≁C → solo (A,B) y (B,C), nunca (A,C) ni un grupo de 3."""
    a = _row(1, "Clase de Cálculo", start_time=time(9, 0), end_time=time(9, 30))
    b = _row(2, "Clase de Cálculo", start_time=time(9, 25), end_time=time(10, 5))
    c = _row(3, "Clase de Cálculo", start_time=time(10, 0), end_time=time(10, 30))
    pairs = mark_duplicates([a, b, c], [], overlap_tolerance=timedelta(0))
    got = {(p.a_id, p.b_id) for p in pairs}
    assert got == {(1, 2), (2, 3)}


# ----- dominio: events_in_range / contribute ------------------------------------- #


def _seed_event(user_id: int, title: str, starts_on: date) -> int:
    # events_in_range lee la vista CONSOLIDADA (no los crudos), así que sembramos ahí.
    with connection() as c:
        eid = c.execute(
            text(
                "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
                "VALUES (:u, :t, :d) RETURNING id"
            ),
            {"u": user_id, "t": title, "d": starts_on},
        ).scalar_one()
    return int(eid)


def test_events_in_range_filters_window_and_user(seed_user2: int) -> None:
    _seed_event(1, "antes", date(2026, 6, 1))
    _seed_event(1, "dentro", date(2026, 6, 15))
    _seed_event(1, "después", date(2026, 6, 30))
    _seed_event(seed_user2, "otro user", date(2026, 6, 15))  # no debe aparecer

    with connection() as c:
        reader = CalendarDomainReader(c, 1)
        events = reader.events_in_range(date(2026, 6, 10), date(2026, 6, 20))

    assert [e.title for e in events] == ["dentro"]
    assert isinstance(events[0], CalendarEvent)


def test_reader_satisfies_domain_protocol(conn: Connection) -> None:
    reader = CalendarDomainReader(conn, 1)
    assert isinstance(reader, CalendarDomain)


def test_contribute_inserts_module_events_with_priority(conn: Connection) -> None:
    reader = CalendarDomainReader(conn, 1)
    n = reader.contribute(
        [
            ContributedEvent(
                title="Clase de Cálculo",
                starts_on=date(2026, 6, 3),
                start_time=time(9, 0),
                priority_rank=1000,
                protected=True,
            )
        ],
        contributed_by="classes",
    )
    assert n == 1
    row = conn.execute(
        text(
            "SELECT origin, priority_rank, protected, contributed_by "
            "FROM mod_calendar_events WHERE user_id = 1"
        )
    ).first()
    assert row is not None
    assert row[0] == "module"
    assert row[1] == 1000
    assert row[2] is True
    assert row[3] == "classes"


def test_contribute_empty_is_noop(conn: Connection) -> None:
    reader = CalendarDomainReader(conn, 1)
    assert reader.contribute([], contributed_by="classes") == 0


# ----- traza jerárquica (ctx.trace) ---------------------------------------------- #


class _NoLLM:
    """Satisface `LLMClient`; `dedup` no toca el LLM (la FASE 2 corre aparte)."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise AssertionError("dedup no debe llamar al LLM")


def _seed_inbox(ext: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
            {"n": ext},
        ).scalar_one()
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :ext, :occ, CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"sid": sid, "ext": ext, "occ": datetime(2026, 6, 3, 12, 0, tzinfo=UTC)},
        ).scalar_one()
    return int(iid)


def test_dedup_emits_trace_entities_and_dedup_decision(conn: Connection) -> None:
    """Con un tracer real, `dedup` emite una ENTIDAD por evento + un paso 'dedup' con la comparación
    'vs evento #other' del par FASE 1 (dos eventos mismo día/hora/título marcan par)."""
    iid = _seed_inbox(ext="cal-trace")
    root = create_root(conn, user_id=1, inbox_id=iid, label="msg")
    tracer = open_module_tracer(
        conn, user_id=1, inbox_id=iid, root_id=root, slug="calendar", label="calendar", seq=0
    )
    ctx = ModuleContext(
        user_id=1,
        conn=conn,
        llm=_NoLLM(),
        deps={},
        summary_id=None,
        inbox_ids=(iid,),
        trace=tracer,
    )
    common = {
        "source_inbox_ids": (iid,),
        "title": "Examen de Análisis",
        "starts_on": date(2026, 6, 3),
        "start_time": time(15, 30),
        "evidence": "examen 3/6 15:30",
    }
    asyncio.run(
        CalendarModule().dedup(ctx, [CalendarEventItem(**common), CalendarEventItem(**common)])
    )

    rows = (
        conn.execute(text("SELECT kind, label FROM trace_nodes WHERE inbox_id = :i"), {"i": iid})
        .mappings()
        .all()
    )
    kinds = [r["kind"] for r in rows]
    assert kinds.count("entity") == 2  # una por evento
    assert "step" in kinds  # el paso 'dedup'
    assert any(r["kind"] == "decision" and str(r["label"]).startswith("vs evento #") for r in rows)
