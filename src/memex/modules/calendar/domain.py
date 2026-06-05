"""Seam `provide_domain` de calendar (ADR-015 §4): el handle tipado del dominio consolidado.

`calendar` es single-writer de `mod_calendar_events`; un módulo dependiente (ej. hackathones,
slice 3) NO lee la tabla con SQL crudo — recibe este handle vía `ctx.deps["calendar"]` y lo
usa con métodos tipados. En este slice se construye el seam de LECTURA (`events_in_range`,
genuinamente útil y testeable); `contribute` queda declarado en la interface pero sin
implementar, porque la resolución de prioridad (`priority_rank`/`protected`/`override_policy`)
y la forma de la contribución se definen recién cuando exista un módulo contribuyente — antes
sería diseño especulativo. El orquestador YA inyecta esto vía `ctx.deps` (`_build_deps`,
commit b5e01c7); falta un módulo consumidor que declare `depends_on=("calendar",)` y lo use.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.engine import Connection


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


@dataclass(frozen=True)
class ContributedEvent:
    """Un evento que un módulo AJENO propone al dominio calendar (capacidad `contribute_domain`).

    Lleva su PRIORIDAD (ADR-015 §4): `priority_rank` alto + `protected=True` (ej. un módulo
    'clases') hace que el evento gane en la consolidación y nada lo sobrescriba —SIN que calendar
    conozca al módulo por nombre (solo respeta el rank/protected/policy que viene acá; el slug se
    guarda en `contributed_by` para auditoría). `override_policy`: 'replace' (default) o
    'fill_only' (solo completa campos vacíos del ganador)."""

    title: str
    starts_on: date
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str = ""
    description: str = ""
    priority_rank: int = 0
    protected: bool = False
    override_policy: str = "replace"


@runtime_checkable
class CalendarDomain(Protocol):
    """Handle tipado del dominio calendar (capacidad `provide_domain`)."""

    def events_in_range(self, start: date, end: date) -> list[CalendarEvent]:
        """Eventos del user cuyo `starts_on` cae en `[start, end]` (inclusive)."""
        ...

    def contribute(self, events: Sequence[ContributedEvent], *, contributed_by: str) -> int:
        """Un módulo ajeno propone eventos al dominio (con su prioridad). Devuelve cuántos puso."""
        ...


class CalendarDomainReader:
    """Implementación del handle ligada a una conexión + user. Lectura pura sobre la tabla."""

    def __init__(self, conn: Connection, user_id: int) -> None:
        self._conn = conn
        self._user_id = user_id

    def events_in_range(self, start: date, end: date) -> list[CalendarEvent]:
        # Lee la vista CONSOLIDADA (canónica, deduplicada y priorizada), no los crudos por-fuente
        # (ADR-015 §4): un módulo dependiente quiere el calendario resuelto, no las copias. Excluye
        # los tombstones (`deleted`). El `event_id` devuelto es el id del consolidado.
        rows = (
            self._conn.execute(
                text(
                    """
                    SELECT id, title, starts_on, ends_on, start_time, end_time, location
                    FROM mod_calendar_consolidated
                    WHERE user_id = :uid AND NOT deleted
                      AND starts_on BETWEEN :start AND :end
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

    def contribute(self, events: Sequence[ContributedEvent], *, contributed_by: str) -> int:
        """Inserta eventos APORTADOS por otro módulo (origin='module') con su prioridad y corre el
        dedup FASE 1 (así se consolidan/deduplican contra el resto). Usa la conexión del handle
        (atómico con lo que haga el orquestador en esa tx). Devuelve cuántos insertó.

        `contributed_by` se guarda como dato de auditoría: calendar respeta la prioridad sin
        conocer al módulo por nombre (ADR-015 §4)."""
        # Import perezoso para no acoplar el import de `domain` con `module` (que es más pesado).
        from memex.modules.calendar.dedup import DedupRow
        from memex.modules.calendar.module import _mark_dedup

        if not events:
            return 0
        new_rows: list[DedupRow] = []
        for e in events:
            event_id = self._conn.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, ends_on, start_time, end_time,
                       location, description, origin, priority_rank, protected, override_policy,
                       contributed_by)
                    VALUES
                      (:uid, ARRAY[]::bigint[], :title, :starts_on, :ends_on, :start_time,
                       :end_time, :location, :description, 'module', :rank, :protected, :policy,
                       :by)
                    RETURNING id
                    """
                ),
                {
                    "uid": self._user_id,
                    "title": e.title,
                    "starts_on": e.starts_on,
                    "ends_on": e.ends_on,
                    "start_time": e.start_time,
                    "end_time": e.end_time,
                    "location": e.location,
                    "description": e.description,
                    "rank": e.priority_rank,
                    "protected": e.protected,
                    "policy": e.override_policy,
                    "by": contributed_by,
                },
            ).scalar_one()
            new_rows.append(
                DedupRow(
                    event_id=int(event_id),
                    title=e.title,
                    location=e.location,
                    starts_on=e.starts_on,
                    ends_on=e.ends_on,
                    start_time=e.start_time,
                    end_time=e.end_time,
                )
            )
        _mark_dedup(self._conn, self._user_id, new_rows)
        return len(events)
