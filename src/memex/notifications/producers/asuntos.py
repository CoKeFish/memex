"""Productor `asuntos_plazo`: un aviso por asunto abierto cuyo `vence` cae en los próximos días.

A la hora configurada, por cada asunto abierto con `due_on` entre hoy y hoy + `dias` emite UN aviso
por plazo (`asunto:<id>:<fecha>`): si el plazo cambia, hay aviso nuevo; si no, no se repite. El
resumen diario ya los lista; esto es el empujón puntual cuando la fecha está encima. Severidad
alta. Siempre plantilla: el estado del asunto ya está redactado.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from memex.modules.asuntos import store as asuntos_store
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec
from memex.notifications.settings import parse_hhmm

NAME = "asuntos_plazo"
KIND = "asuntos.vence"


def _cuando(due: date, today: date) -> str:
    delta = (due - today).days
    if delta <= 0:
        return "Vence hoy"
    if delta == 1:
        return "Vence mañana"
    return f"Vence en {delta} días"


async def produce(ctx: ProducerContext) -> list[Draft]:
    hora = parse_hhmm(str(ctx.config["hora"]))
    if ctx.now.time() < hora:
        return []
    today = ctx.now.date()
    limite = today + timedelta(days=int(ctx.config["dias"]))
    out: list[Draft] = []
    for a in asuntos_store.list_asuntos(ctx.conn, ctx.user_id, statuses=("abierto",), limit=500):
        if a.due_on is None or a.due_on < today or a.due_on > limite:
            continue
        parts = [f"{_cuando(a.due_on, today)} ({a.due_on:%d/%m})"]
        if a.state_text:
            parts.append(a.state_text)
        out.append(
            Draft(
                kind=KIND,
                severity="alta",
                dedup_key=f"asunto:{a.id}:{a.due_on.isoformat()}",
                title=f"{_cuando(a.due_on, today)}: {a.name}",
                body=" · ".join(parts),
                facts={
                    "asunto_id": a.id,
                    "name": a.name,
                    "due_on": a.due_on.isoformat(),
                    "priority": a.priority,
                    "kind": a.kind,
                },
                deep_link=f"/asuntos/{a.id}",
                expires_at=datetime.combine(a.due_on, time(23, 59, 59), tzinfo=ctx.now.tzinfo),
            )
        )
    return out


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description="Un aviso por asunto abierto cuyo plazo cae en los próximos días.",
    default_enabled=True,
    default_render="plantilla",
    default_config={"hora": "08:00", "dias": 3},
    produce=produce,
    llm_allowed=False,
)
