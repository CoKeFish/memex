"""Recurrencia en las LECTURAS y en el proveedor: el dominio (transporte) expande maestros, los
conflictos se detectan por ocurrencia, la API expone la regla, Google entrega maestros +
excepciones y el pull las persiste."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar

import pytest
from sqlalchemy import text

from memex.core.source import HealthResult
from memex.db import connection
from memex.modules.calendar.cli import main as cal_main
from memex.modules.calendar.domain import CalendarDomainReader
from memex.modules.calendar.providers.base import (
    ProviderEvent,
    ProviderEventRef,
    ProviderEventWrite,
    ProviderPage,
)
from memex.modules.calendar.providers.google import _parse_event
from memex.modules.calendar.sync import run_pull


def _add_master() -> int:
    assert (
        cal_main(
            [
                "add",
                "--title",
                "Programa Intensivo B1",
                "--date",
                "2026-09-02",
                "--time",
                "15:10",
                "--end-time",
                "17:00",
                "--every",
                "weekly",
                "--byday",
                "MO,TU,WE,TH,FR",
                "--until",
                "2026-09-25",
                "--location",
                "Sede Centro",
                "--fijado",
            ]
        )
        == 0
    )
    with connection() as c:
        return int(
            c.execute(
                text("SELECT id FROM mod_calendar_consolidated WHERE cardinality(recurrence) > 0")
            ).scalar_one()
        )


def test_domain_reader_expands_master_into_occurrences() -> None:
    """El transporte («¿llego a tiempo?») lee por `events_in_range`: un maestro entra una vez por
    ocurrencia dentro del rango, con `starts_on` = el día; el sábado no hay clase."""
    cons_id = _add_master()
    with connection() as c:
        week = CalendarDomainReader(c, 1).events_in_range(date(2026, 9, 14), date(2026, 9, 20))
    assert [e.starts_on for e in week] == [date(2026, 9, 14 + i) for i in range(5)]
    assert all(e.event_id == cons_id and e.start_time == time(15, 10) for e in week)
    assert week[0].series_id is not None and week[0].recurrence
    assert week[0].ends_on is None  # por ocurrencia: un día


def _next_monday(after_days: int = 7) -> date:
    """El lunes que viene después de `after_days` días: los conflictos solo se detectan dentro de
    un horizonte alrededor de HOY (consolidate.CONFLICT_HORIZON_*), así que este test no puede
    usar fechas fijas (falló solo desde el 2026-09-23, cuando el 16 quedó fuera del horizonte)."""
    d = date.today() + timedelta(days=after_days)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def test_conflicts_detected_per_occurrence_with_day() -> None:
    """Una cita protegida un miércoles a las 15:30 choca con la clase de ese día (y solo con esa
    ocurrencia): el conflicto guarda el día concreto. Fechas relativas a hoy."""
    lunes = _next_monday()
    miercoles, sabado, fin = (lunes + timedelta(days=n) for n in (2, 5, 25))
    assert (
        cal_main(
            [
                "add",
                "--title",
                "Clase de la tarde",
                "--date",
                lunes.isoformat(),
                "--time",
                "15:10",
                "--end-time",
                "17:00",
                "--every",
                "weekly",
                "--byday",
                "MO,TU,WE,TH,FR",
                "--until",
                fin.isoformat(),
                "--fijado",
            ]
        )
        == 0
    )
    with connection() as c:
        cons_id = int(
            c.execute(
                text("SELECT id FROM mod_calendar_consolidated WHERE cardinality(recurrence) > 0")
            ).scalar_one()
        )
    assert (
        cal_main(
            [
                "add",
                "--title",
                "Odontología",
                "--date",
                miercoles.isoformat(),
                "--time",
                "15:30",
                "--end-time",
                "16:00",
                "--fijado",
            ]
        )
        == 0
    )
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT consolidated_a_id, consolidated_b_id, occurs_on, status "
                "FROM mod_calendar_conflicts"
            )
        ).all()
    assert len(rows) == 1
    a, b, day, status = rows[0]
    assert cons_id in (a, b) and day == miercoles and status == "pending"
    # una cita un sábado a esa hora NO choca: la serie no tiene ocurrencia
    assert (
        cal_main(
            [
                "add",
                "--title",
                "Peluquería",
                "--date",
                sabado.isoformat(),
                "--time",
                "15:30",
                "--fijado",
            ]
        )
        == 0
    )
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_conflicts")).scalar() == 1


def test_api_events_expose_rule_and_next_occurrences(client: Any) -> None:
    cons_id = _add_master()
    items = client.get("/calendar/events").json()["items"]
    row = next(i for i in items if i["id"] == cons_id)
    assert row["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"]
    assert row["recurrence_human"] == "semanal (lun a vie) hasta 2026-09-25"
    assert row["series_id"].startswith("memex:")
    assert isinstance(row["next_occurrences"], list)
    # el front pinta una entrada por ocurrencia dentro del horizonte (la serie es de 2026-09;
    # si hoy ya pasó el horizonte, la lista queda vacía y no se fuerza)
    assert isinstance(row["occurrences"], list)
    assert all(str(d) >= "2026-09-02" for d in row["occurrences"])


def test_google_parse_master_and_exception() -> None:
    master = _parse_event(
        {
            "id": "m1",
            "status": "confirmed",
            "summary": "Clase de inglés",
            "start": {"dateTime": "2026-09-02T15:10:00-05:00"},
            "end": {"dateTime": "2026-09-02T17:00:00-05:00"},
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925T045959Z"],
        },
        "m1",
    )
    assert master is not None
    assert master.recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925T045959Z",)
    assert master.recurring_event_id is None and master.original_start_on is None
    exc = _parse_event(
        {
            "id": "m1_20260916T201000Z",
            "status": "confirmed",
            "summary": "Clase de inglés (movida)",
            "recurringEventId": "m1",
            "originalStartTime": {"dateTime": "2026-09-16T15:10:00-05:00"},
            "start": {"dateTime": "2026-09-16T16:00:00-05:00"},
            "end": {"dateTime": "2026-09-16T17:30:00-05:00"},
        },
        "m1_20260916T201000Z",
    )
    assert exc is not None
    assert exc.recurrence == () and exc.recurring_event_id == "m1"
    assert exc.original_start_on == date(2026, 9, 16) and exc.original_start_time == time(15, 10)
    assert exc.starts_on == date(2026, 9, 16) and exc.start_time == time(16, 0)


class _Fake:
    name: ClassVar[str] = "google"

    def __init__(self, page: ProviderPage) -> None:
        self._page = page

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="fake", checked_at=datetime.now(UTC))

    async def list_delta(
        self, *, sync_token: str | None = None, page_token: str | None = None
    ) -> ProviderPage:
        return self._page

    async def create_event(self, ev: ProviderEventWrite) -> ProviderEventRef:
        raise NotImplementedError

    async def update_event(
        self, *, provider_event_id: str, etag: str | None, ev: ProviderEventWrite
    ) -> ProviderEventRef:
        raise NotImplementedError

    async def delete_event(self, *, provider_event_id: str, etag: str | None) -> None:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_pull_persists_master_rule_and_exception_and_list_expands() -> None:
    with connection() as c:
        aid = int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_provider_accounts "
                    "(user_id, provider, account_label, calendar_id, token_path_env) "
                    "VALUES (1, 'google', 'me@gmail.com', 'primary', 'CAL_TOKEN_PATH') RETURNING id"
                )
            ).scalar_one()
        )
    master = ProviderEvent(
        provider_event_id="m1",
        title="Clase de inglés",
        starts_on=date(2026, 9, 2),
        start_time=time(15, 10),
        end_time=time(17, 0),
        etag="e1",
        recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925",),
    )
    exc = ProviderEvent(
        provider_event_id="m1_x",
        title="Clase de inglés (movida)",
        starts_on=date(2026, 9, 16),
        start_time=time(16, 0),
        end_time=time(17, 30),
        etag="e2",
        recurring_event_id="m1",
        original_start_on=date(2026, 9, 16),
        original_start_time=time(15, 10),
    )
    stats = await run_pull(
        1, aid, client=_Fake(ProviderPage(events=(master, exc), next_sync_token="T"))
    )
    assert stats.created == 2
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT provider_event_id, recurrence, recurring_event_id, original_start_on "
                "FROM mod_calendar_events ORDER BY id"
            )
        ).all()
    assert rows[0][1] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"]
    assert rows[1][2] == "m1" and rows[1][3] == date(2026, 9, 16)
    # la consolidación conserva la regla del maestro y la serie de la excepción; la lectura expande
    from memex.modules.calendar.consolidate import run_consolidation
    from memex.modules.calendar.manual import list_events

    run_consolidation(1)
    items = list_events(1, since=date(2026, 9, 14), until=date(2026, 9, 18))
    assert [(i["starts_on"], i["start_time"], i["is_exception"]) for i in items] == [
        ("2026-09-14", "15:10", False),
        ("2026-09-15", "15:10", False),
        ("2026-09-16", "16:00", True),
        ("2026-09-17", "15:10", False),
        ("2026-09-18", "15:10", False),
    ]
