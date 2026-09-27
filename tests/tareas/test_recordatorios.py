"""Recordatorios del calendario → tareas: un evento de día completo cuyo título es una acción
(«Pagar arriendo…») se materializa como tarea por ocurrencia dentro del horizonte, una sola vez
(ni completada ni cancelada lo resucitan); cumpleaños, reuniones con hora y anuncios no."""

from __future__ import annotations

from datetime import date, time
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.modules.tareas import module as tareas
from memex.modules.tareas.recordatorios import (
    action_verb,
    materialize_calendar_reminders,
)


def _consolidado(
    title: str,
    starts_on: date,
    *,
    recurrence: list[str] | None = None,
    start_time: time | None = None,
    commitment: str = "confirmado",
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated "
                    "(user_id, title, starts_on, start_time, commitment, recurrence, series_id) "
                    "VALUES (1, :t, :d, :st, :cm, CAST(:rec AS TEXT[]), :sid) RETURNING id"
                ),
                {
                    "t": title,
                    "d": starts_on,
                    "st": start_time,
                    "cm": commitment,
                    "rec": recurrence or [],
                    "sid": f"serie-{uuid4().hex[:8]}" if recurrence else None,
                },
            ).scalar_one()
        )


def _tareas_de(event_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT id, title, due_on, status, category, origin, metadata FROM mod_tareas "
                    "WHERE user_id = 1 AND metadata->>'calendar_event_id' = :e ORDER BY due_on"
                ),
                {"e": str(event_id)},
            ).mappings()
        ]


def test_action_verb() -> None:
    assert action_verb("Pagar arriendo y administración") == "pagar"
    assert action_verb("  RENOVAR el pasaporte") == "renovar"
    assert action_verb("Santiago Mesa's birthday") is None
    assert action_verb("¡Feliz cumpleaños!") is None
    assert action_verb("Reunión de equipo") is None


def test_el_recordatorio_mensual_nace_como_tarea_una_vez_por_ocurrencia() -> None:
    ev = _consolidado(
        f"Pagar arriendo {uuid4().hex[:4]}",
        date(2026, 4, 26),
        recurrence=["RRULE:FREQ=MONTHLY;BYMONTHDAY=26"],
    )
    hoy = date(2026, 9, 22)
    with connection() as c:
        st = materialize_calendar_reminders(c, 1, today=hoy, apply=False)
        assert st.considered == 1 and st.created == 1 and st.existing == 0
        assert _tareas_de(ev) == []  # dry-run no escribe
        st = materialize_calendar_reminders(c, 1, today=hoy)
    assert st.created == 1
    (t,) = _tareas_de(ev)
    assert t["due_on"] == date(2026, 9, 26) and t["category"] == "pago"
    assert t["origin"] == "calendar" and t["metadata"]["occurrence"] == "2026-09-26"
    assert t["metadata"]["due_source"] == "calendar" and t["metadata"]["series_id"]
    # idempotente
    with connection() as c:
        st = materialize_calendar_reminders(c, 1, today=hoy)
    assert st.created == 0 and st.existing == 1 and len(_tareas_de(ev)) == 1
    # completada o cancelada: no vuelve a nacer esa ocurrencia; la del mes siguiente sí
    with connection() as c:
        tareas.set_status(c, 1, int(t["id"]), "completada")
        st = materialize_calendar_reminders(c, 1, today=hoy)
        assert st.created == 0
        st = materialize_calendar_reminders(c, 1, today=date(2026, 10, 20))
    assert st.created == 1
    assert [x["due_on"] for x in _tareas_de(ev)] == [date(2026, 9, 26), date(2026, 10, 26)]
    # fuera del horizonte (a 20 días) todavía no
    with connection() as c:
        assert materialize_calendar_reminders(c, 1, today=date(2026, 11, 1)).created == 0


def test_lo_que_pasa_no_es_recordatorio() -> None:
    cumple = _consolidado(
        "Santiago Mesa's birthday", date(2026, 9, 24), recurrence=["RRULE:FREQ=YEARLY"]
    )
    reunion = _consolidado(
        "Pagar la cuota en la reunión", date(2026, 9, 24), start_time=time(10, 0)
    )
    anuncio = _consolidado("Pagar la inscripción", date(2026, 9, 24), commitment="anuncio")
    unico = _consolidado(f"Renovar el SOAT {uuid4().hex[:4]}", date(2026, 9, 25))
    with connection() as c:
        st = materialize_calendar_reminders(c, 1, today=date(2026, 9, 22))
    assert _tareas_de(cumple) == [] and _tareas_de(reunion) == [] and _tareas_de(anuncio) == []
    (t,) = _tareas_de(unico)
    assert t["category"] == "tramite" and t["due_on"] == date(2026, 9, 25)
    assert st.created >= 1
