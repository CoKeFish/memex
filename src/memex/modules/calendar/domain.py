"""Seam `provide_domain` de calendar (ADR-015 §4): el handle tipado del dominio consolidado.

`calendar` es single-writer de `mod_calendar_events`; un módulo dependiente (ej. hackathones,
slice 3) NO lee la tabla con SQL crudo — recibe este handle vía `ctx.deps["calendar"]` y lo
usa con métodos tipados. En este slice se construye el seam de LECTURA (`events_in_range`,
genuinamente útil y testeable); `contribute` queda declarado en la interface pero sin
implementar, porque la resolución de prioridad (`priority_rank`/`protected`/`override_policy`)
y la forma de la contribución se definen recién cuando exista un módulo contribuyente — antes
sería diseño especulativo. El orquestador todavía NO inyecta esto (`ctx.deps={}`); el wiring
es trabajo de slice 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.modules.calendar.schema import CalendarEventItem


@dataclass(frozen=True)
class CalendarEvent:
    """Un evento consolidado, tal como lo lee un módulo dependiente del dominio calendar."""

    event_id: int
    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str


@runtime_checkable
class CalendarDomain(Protocol):
    """Handle tipado del dominio calendar (capacidad `provide_domain`)."""

    def events_in_range(self, start: date, end: date) -> list[CalendarEvent]:
        """Eventos del user cuyo `starts_on` cae en `[start, end]` (inclusive)."""
        ...

    def contribute(self, events: Sequence[CalendarEventItem]) -> int:
        """Un módulo ajeno propone eventos al dominio (con su prioridad). Seam futuro."""
        ...


class CalendarDomainReader:
    """Implementación del handle ligada a una conexión + user. Lectura pura sobre la tabla."""

    def __init__(self, conn: Connection, user_id: int) -> None:
        self._conn = conn
        self._user_id = user_id

    def events_in_range(self, start: date, end: date) -> list[CalendarEvent]:
        rows = (
            self._conn.execute(
                text(
                    """
                    SELECT id, title, starts_on, ends_on, start_time, end_time, location
                    FROM mod_calendar_events
                    WHERE user_id = :uid AND starts_on BETWEEN :start AND :end
                    ORDER BY starts_on, start_time NULLS FIRST, id
                    """
                ),
                {"uid": self._user_id, "start": start, "end": end},
            )
            .mappings()
            .all()
        )
        return [
            CalendarEvent(
                event_id=int(r["id"]),
                title=str(r["title"]),
                starts_on=r["starts_on"],
                ends_on=r["ends_on"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                location=str(r["location"]),
            )
            for r in rows
        ]

    def contribute(self, events: Sequence[CalendarEventItem]) -> int:
        raise NotImplementedError(
            "contribute: requiere un módulo contribuyente para definir prioridad/forma "
            "(ADR-015 §4) — se implementa en el slice de dependencias (hackathones)"
        )
