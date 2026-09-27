"""Productor `recordatorio_evento`: «en N minutos: <evento>» para los eventos con hora.

Mira los eventos de hoy y mañana (la anticipación puede cruzar la medianoche) cuyo nivel de
compromiso está en `compromisos` (default confirmado y provisional; los anuncios no se recuerdan),
y avisa cuando el inicio cae dentro de los próximos `anticipacion_min` minutos. Los de todo el día
no tienen hora: los cubre el resumen diario. Siempre plantilla (no hay nada que redactar).
Idempotente por ocurrencia: `evento:<id>:<fecha>`; el aviso vence al empezar el evento.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from memex.modules.calendar.manual import list_events
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec

NAME = "recordatorio_evento"
KIND = "calendar.recordatorio"


def _start_of(e: dict[str, Any], ctx: ProducerContext) -> datetime | None:
    hhmm = e.get("start_time")
    if not hhmm:
        return None
    hh, mm = str(hhmm).split(":")
    day = datetime.fromisoformat(str(e["starts_on"])).date()
    return datetime.combine(day, time(int(hh), int(mm)), tzinfo=ctx.now.tzinfo)


async def produce(ctx: ProducerContext) -> list[Draft]:
    lead = timedelta(minutes=int(ctx.config["anticipacion_min"]))
    allowed = set(ctx.config["compromisos"])
    today = ctx.now.date()
    events = list_events(ctx.user_id, since=today, until=today + timedelta(days=1), limit=200)
    out: list[Draft] = []
    for e in events:
        if e.get("commitment") not in allowed:
            continue
        start = _start_of(e, ctx)
        if start is None or not (ctx.now <= start <= ctx.now + lead):
            continue
        minutes = max(1, int((start - ctx.now).total_seconds() // 60))
        when = e["start_time"] + (f"-{e['end_time']}" if e.get("end_time") else "")
        where = f" · {e['location']}" if e.get("location") else ""
        flag = "" if e.get("commitment") == "confirmado" else f" ({e.get('commitment')})"
        out.append(
            Draft(
                kind=KIND,
                severity="info",
                dedup_key=f"evento:{e['id']}:{e['starts_on']}",
                title=f"En {minutes} min: {e['title']}",
                body=f"{when}{where}{flag}",
                facts={
                    "event_id": e["id"],
                    "title": e["title"],
                    "starts_on": e["starts_on"],
                    "start_time": e["start_time"],
                    "end_time": e.get("end_time"),
                    "location": e.get("location"),
                    "commitment": e.get("commitment"),
                },
                deep_link="/calendario",
                expires_at=start,
            )
        )
    return out


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description="Recordatorio N minutos antes de cada evento con hora (confirmado o provisional).",
    default_enabled=True,
    default_render="plantilla",
    default_config={"anticipacion_min": 60, "compromisos": ["confirmado", "provisional"]},
    produce=produce,
    llm_allowed=False,
)
