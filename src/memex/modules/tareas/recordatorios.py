"""Recordatorios del calendario → tareas (2026-09-26).

Por qué: «Pagar arriendo y administración» vive en Google Calendar como evento recurrente de día
completo. El módulo de tareas solo extraía de mensajes, así que nunca aparecía entre los
pendientes aunque es, claramente, algo que HACER. La persona guarda en el calendario cosas de dos
clases: lo que PASA (reunión, clase, cumpleaños) y lo que tiene que HACER (pagar, renovar,
enviar). Lo segundo es tarea.

Regla determinista, sin LLM: un evento consolidado de DÍA COMPLETO (sin hora), confirmado, cuyo
título empieza con un verbo de acción en infinitivo («Pagar…», «Renovar…», «Enviar…») es un
recordatorio; cada ocurrencia dentro del horizonte (`HORIZONTE_DIAS`) se materializa como una
tarea con esa fecha límite, origen `calendar`, y categoría según el verbo. Una ocurrencia se
materializa UNA sola vez (`metadata.calendar_event_id` + `metadata.occurrence`): completarla o
cancelarla no la resucita; vencida sin hacer sigue pendiente y atrasada (un arriendo no se
«vence»: sigue debiéndose). Lo lee el ciclo de tareas del scheduler (`run_tareas_cycle`) y el
CLI `memex-tareas recordatorios`. Lee el calendario por su handle de dominio
(`CalendarDomainReader`), nunca la tabla cruda (ADR-015 §4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.calendar.domain import CalendarDomainReader, CalendarEvent
from memex.modules.tareas.module import add

_log = get_logger("memex.modules.tareas.recordatorios")

#: Cuántos días hacia adelante se materializan ocurrencias (una tarea para «pagar el 26»
#: aparece una semana antes, no el mismo día).
HORIZONTE_DIAS = 7
#: Verbo inicial → categoría. Un título que empieza con uno de estos verbos es una acción.
VERBO_CATEGORIA: dict[str, str] = {
    # pago
    "pagar": "pago",
    "cobrar": "pago",
    "consignar": "pago",
    "transferir": "pago",
    "recargar": "pago",
    "abonar": "pago",
    # trámite
    "renovar": "tramite",
    "solicitar": "tramite",
    "declarar": "tramite",
    "tramitar": "tramite",
    "firmar": "tramite",
    "reclamar": "tramite",
    "recoger": "tramite",
    "devolver": "tramite",
    "agendar": "tramite",
    "reservar": "tramite",
    "confirmar": "tramite",
    "cancelar": "tramite",
    "actualizar": "tramite",
    "cambiar": "tramite",
    # entrega
    "enviar": "entrega",
    "entregar": "entrega",
    "subir": "entrega",
    "presentar": "entrega",
    "publicar": "entrega",
    "mandar": "entrega",
    # inscripción
    "inscribir": "inscripcion",
    "inscribirse": "inscripcion",
    "registrarse": "inscripcion",
    "postular": "inscripcion",
    "postularse": "inscripcion",
    "anotarse": "inscripcion",
    # otro (acción sin categoría propia)
    "llamar": "otro",
    "comprar": "otro",
    "revisar": "otro",
    "hacer": "otro",
    "llevar": "otro",
    "sacar": "otro",
    "pedir": "otro",
    "avisar": "otro",
    "escribir": "otro",
    "leer": "otro",
    "limpiar": "otro",
    "organizar": "otro",
}
_FIRST_WORD = re.compile(r"^\W*([a-záéíóúñü]+)", re.IGNORECASE)


def action_verb(title: str) -> str | None:
    """El verbo de acción con el que empieza el título, o None si no es una acción."""
    m = _FIRST_WORD.match(title.strip().lower())
    if not m:
        return None
    verb = m.group(1)
    return verb if verb in VERBO_CATEGORIA else None


def is_reminder(ev: CalendarEvent) -> bool:
    """Día completo, confirmado y título de acción."""
    return (
        ev.start_time is None
        and ev.commitment == "confirmado"
        and action_verb(ev.title) is not None
    )


@dataclass
class RecordatoriosStats:
    considered: int = 0  #: ocurrencias de recordatorios dentro del horizonte
    created: int = 0
    existing: int = 0  #: ya materializadas (cualquier estado)
    items: list[dict[str, Any]] = field(default_factory=list)


def _existing(conn: Connection, user_id: int, event_id: int, day: date) -> int | None:
    val = conn.execute(
        text(
            "SELECT id FROM mod_tareas WHERE user_id = :u "
            "AND metadata->>'calendar_event_id' = :e AND metadata->>'occurrence' = :d "
            "ORDER BY id LIMIT 1"
        ),
        {"u": user_id, "e": str(event_id), "d": day.isoformat()},
    ).scalar()
    return int(val) if val is not None else None


def materialize_calendar_reminders(
    conn: Connection,
    user_id: int,
    *,
    today: date,
    horizon_days: int = HORIZONTE_DIAS,
    apply: bool = True,
) -> RecordatoriosStats:
    """Una tarea por ocurrencia de recordatorio entre `today` y `today + horizon_days`. Idempotente:
    la ocurrencia ya materializada (en cualquier estado) no se repite. `apply=False` solo cuenta."""
    st = RecordatoriosStats()
    reader = CalendarDomainReader(conn, user_id)
    for ev in reader.events_in_range(today, today + timedelta(days=horizon_days)):
        if not is_reminder(ev):
            continue
        st.considered += 1
        verb = action_verb(ev.title) or ""
        existing = _existing(conn, user_id, ev.event_id, ev.starts_on)
        item: dict[str, Any] = {
            "event_id": ev.event_id,
            "occurrence": ev.starts_on.isoformat(),
            "title": ev.title,
            "category": VERBO_CATEGORIA.get(verb, "otro"),
        }
        if existing is not None:
            st.existing += 1
            item["tarea_id"] = existing
            st.items.append(item)
            continue
        if apply:
            row = add(
                conn,
                user_id,
                title=ev.title,
                due_on=ev.starts_on,
                category=VERBO_CATEGORIA.get(verb, "otro"),
                origin="calendar",
                force=True,  # la identidad es (evento, ocurrencia): el dedup por título no aplica
                metadata={
                    "calendar_event_id": ev.event_id,
                    "occurrence": ev.starts_on.isoformat(),
                    "series_id": ev.series_id,
                    "due_source": "calendar",
                },
            )
            item["tarea_id"] = int(row["id"])
            _log.info(
                "tareas.recordatorio.created",
                user_id=user_id,
                tarea_id=int(row["id"]),
                event_id=ev.event_id,
                occurrence=ev.starts_on.isoformat(),
                title=ev.title[:80],
            )
        st.created += 1
        st.items.append(item)
    return st


def as_json(st: RecordatoriosStats) -> str:
    return json.dumps(
        {
            "considered": st.considered,
            "created": st.created,
            "existing": st.existing,
            "items": st.items,
        },
        ensure_ascii=False,
    )
