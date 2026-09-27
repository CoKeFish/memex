"""Productor `tareas_del_dia`: a la hora configurada, un aviso por tarea pendiente que vence HOY.

Las vencidas de días anteriores no se repiten acá (las lista el resumen diario cada mañana): un
recordatorio por tarea y por fecha límite, `tarea:<id>:<fecha>`. Severidad alta si la tarea es de
prioridad alta. Siempre plantilla.
"""

from __future__ import annotations

from datetime import datetime, time

from memex.modules.tareas.module import list_tareas
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec
from memex.notifications.settings import parse_hhmm

NAME = "tareas_del_dia"
KIND = "tareas.vence_hoy"


async def produce(ctx: ProducerContext) -> list[Draft]:
    hora = parse_hhmm(str(ctx.config["hora"]))
    if ctx.now.time() < hora:
        return []
    today = ctx.now.date()
    end_of_day = datetime.combine(today, time(23, 59, 59), tzinfo=ctx.now.tzinfo)
    out: list[Draft] = []
    for t in list_tareas(ctx.conn, ctx.user_id, due_before=today, limit=200):
        if t["due_on"] != today.isoformat():
            continue
        parts = []
        if t.get("description"):
            parts.append(str(t["description"]))
        if t.get("asunto_name"):
            parts.append(f"Asunto: {t['asunto_name']}")
        parts.append(f"Prioridad {t['priority']}")
        out.append(
            Draft(
                kind=KIND,
                severity="alta" if t["priority"] == "alta" else "info",
                dedup_key=f"tarea:{t['id']}:{t['due_on']}",
                title=f"Vence hoy: {t['title']}",
                body=" · ".join(parts),
                facts={
                    "tarea_id": t["id"],
                    "title": t["title"],
                    "due_on": t["due_on"],
                    "priority": t["priority"],
                    "asunto": t.get("asunto_name"),
                },
                deep_link="/tareas",
                expires_at=end_of_day,
            )
        )
    return out


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description="Un aviso por tarea pendiente que vence hoy, a la hora configurada.",
    default_enabled=True,
    default_render="plantilla",
    default_config={"hora": "08:00"},
    produce=produce,
    llm_allowed=False,
)
