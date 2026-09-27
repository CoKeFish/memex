"""Productor `resumen_diario`: una vez al día, a la hora configurada, el panorama del día.

Junta lo que memex ya sabe: eventos de hoy (calendario consolidado, recurrencias expandidas),
tareas vencidas y que vencen hoy o en los próximos días, asuntos con MOTIVO para salir hoy, y
pagos fallidos sin regularizar. Plantilla en español por secciones; con `render=llm` el modelo
redacta a partir de los mismos hechos (default, es el aviso que más gana con prosa).
Idempotente por día: `resumen:<fecha>`.

Cadencia (2026-09-26, pedido del dueño: «hay cosas que no me interesa recordar todos los días,
como lo de la condonación»). Antes todo asunto de prioridad alta salía a diario, idéntico, y un
pago fallido se repetía cada mañana. Ahora:
- Un asunto entra al resumen diario solo con motivo: vence dentro de `dias_asuntos`, o tuvo una
  NOVEDAD desde el resumen anterior (cambió su estado, `state_updated_at` en las últimas 24 h).
  Prioridad alta sin motivo no se repite a diario.
- Lo VIEJO vuelve con cadencia, no a diario: una tarea vencida y un pago fallido salen los
  primeros días y después cada semana, cada mes (`CADENCIA_DIAS`).
- Un día por semana (`dia_panorama`, 0 = lunes) el resumen trae el PANORAMA: todos los asuntos
  abiertos de prioridad alta y todo lo vencido, en una línea compacta, para que nada quede
  olvidado del todo.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from memex.modules.asuntos import store as asuntos_store
from memex.modules.calendar.manual import list_events
from memex.modules.finance.manual import pending_payments
from memex.modules.tareas.agenda import AgendaItem, agenda
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec
from memex.notifications.settings import parse_hhmm

NAME = "resumen_diario"
KIND = "resumen.diario"

_BRIEF = (
    "Redacta el resumen matutino del día para el usuario a partir de los hechos. Español, directo, "
    "sin saludo ni cierre, sin inventar nada que no esté en los hechos. Empieza por lo de hoy "
    "(eventos con su hora) y lo vencido; luego qué sigue: tareas próximas y entregas de proyectos "
    "con su avance, asuntos con su motivo (vence, novedad) y pagos pendientes. Si hay "
    "«panorama», es la lista semanal de lo abierto: una sola línea compacta. Viñetas cortas; si "
    "una sección está vacía, omítela. Si no hay nada, dilo en una línea. Máximo 12 líneas."
)

#: Días (desde que venció / desde el pago fallido) en los que algo viejo vuelve a mencionarse: a
#: diario los primeros días, después cada semana, después cada mes. Fuera de estos días, solo el
#: panorama semanal lo trae.
CADENCIA_DIAS: frozenset[int] = frozenset({0, 1, 2, 3, 7, 14, 21, 30})
#: Ventana de «novedad» de un asunto: cambió su estado desde el resumen de ayer.
NOVEDAD_HORAS = 24


def en_cadencia(dias: int) -> bool:
    return dias in CADENCIA_DIAS or (dias > 30 and dias % 30 == 0)


def _day_bounds(d: date, ctx: ProducerContext) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=ctx.now.tzinfo)


def _fmt_event(e: dict[str, Any]) -> str:
    when = e.get("start_time") or "todo el día"
    if e.get("start_time") and e.get("end_time"):
        when = f"{e['start_time']}-{e['end_time']}"
    where = f" · {e['location']}" if e.get("location") else ""
    flag = "" if e.get("commitment") == "confirmado" else f" ({e.get('commitment')})"
    return f"• {when} {e['title']}{where}{flag}"


def _fmt_item(i: AgendaItem, today: date) -> str:
    """Una tarea por su vencimiento; un proyecto por su entrega y avance (los pasos no se listan
    uno por uno: es la misma agenda «qué sigue» de la ventana del asunto)."""
    asunto = f" — {i.asunto_name}" if i.asunto_name else ""
    if i.kind == "entrega":
        extra = f", {i.detail}" if i.detail else ""
        when = "entrega vencida el" if i.late else "entrega"
        return f"• {i.title}: {when} {i.date:%d/%m}{extra}{asunto}"
    if i.late:
        tag = f" (vencida el {i.date:%d/%m})"
    elif i.date == today:
        tag = " (hoy)"
    else:
        tag = f" ({i.date:%d/%m})"
    extra = f" · {i.detail}" if i.detail else ""
    return f"• {i.title}{tag}{extra}{asunto}"


def motivo_asunto(a: asuntos_store.Asunto, *, limite: date, since: datetime) -> str | None:
    """Por qué este asunto merece salir HOY; None = no sale (aunque sea de prioridad alta)."""
    if a.due_on is not None and a.due_on <= limite:
        return f"vence {a.due_on:%d/%m}"
    if a.state_updated_at is not None and a.state_updated_at >= since and a.state_text:
        return f"novedad: {' '.join(a.state_text.split())[:90]}"
    return None


def _fmt_asunto(a: asuntos_store.Asunto, motivo: str) -> str:
    return f"• {a.name} [{a.priority}] · {motivo}"


def _fmt_pago(p: dict[str, Any]) -> str:
    amount = f"{p['amount']:,.0f} {p['currency']}"
    who = p["counterparty"] or "sin contraparte"
    return f"• {who}: {amount} ({p['status']}, {p['age_days']} d)"


async def produce(ctx: ProducerContext) -> list[Draft]:
    hora = parse_hhmm(str(ctx.config["hora"]))
    if ctx.now.time() < hora:
        return []
    today = ctx.now.date()
    dias_tareas = int(ctx.config["dias_tareas"])
    dias_asuntos = int(ctx.config["dias_asuntos"])
    panorama_hoy = today.weekday() == int(ctx.config.get("dia_panorama", 0))
    since = ctx.now - timedelta(hours=NOVEDAD_HORAS)

    eventos = list_events(ctx.user_id, since=today, until=today, limit=50)
    eventos = [e for e in eventos if e.get("commitment") != "anuncio"]
    items = agenda(ctx.conn, ctx.user_id, today=today, days=dias_tareas)
    # Lo vencido: a diario los primeros días, después con cadencia; el panorama lo trae todo.
    vencidas = [i for i in items if i.late and (panorama_hoy or en_cadencia((today - i.date).days))]
    hoy = [i for i in items if i.date == today]
    proximas = [i for i in items if i.date > today]
    abiertos = asuntos_store.list_asuntos(ctx.conn, ctx.user_id, statuses=("abierto",), limit=200)
    limite = today + timedelta(days=dias_asuntos)
    con_motivo: list[tuple[asuntos_store.Asunto, str]] = []
    for a in abiertos:
        m = motivo_asunto(a, limite=limite, since=since)
        if m:
            con_motivo.append((a, m))
    con_motivo.sort(key=lambda am: (am[0].priority != "alta", am[0].due_on or date.max, am[0].name))
    listados = {a.id for a, _ in con_motivo}
    panorama = (
        sorted(
            (a for a in abiertos if a.priority == "alta" and a.id not in listados),
            key=lambda a: a.name,
        )
        if panorama_hoy
        else []
    )
    pagos_todos = pending_payments(ctx.conn, ctx.user_id, limit=20)
    pagos = [p for p in pagos_todos if panorama_hoy or en_cadencia(int(p["age_days"]))]

    sections: list[str] = []
    if eventos:
        sections.append("Hoy:\n" + "\n".join(_fmt_event(e) for e in eventos))
    if vencidas:
        sections.append("Tareas vencidas:\n" + "\n".join(_fmt_item(i, today) for i in vencidas))
    if hoy:
        sections.append("Tareas para hoy:\n" + "\n".join(_fmt_item(i, today) for i in hoy))
    if proximas:
        sections.append("Próximas tareas:\n" + "\n".join(_fmt_item(i, today) for i in proximas))
    if con_motivo:
        sections.append("Asuntos a seguir:\n" + "\n".join(_fmt_asunto(a, m) for a, m in con_motivo))
    if panorama:
        sections.append(
            "Panorama de la semana (prioridad alta): " + "; ".join(a.name for a in panorama)
        )
    if pagos:
        sections.append("Pagos sin regularizar:\n" + "\n".join(_fmt_pago(p) for p in pagos))
    body = (
        "\n\n".join(sections)
        if sections
        else "Nada pendiente: sin eventos hoy, sin tareas que venzan ni pagos pendientes."
    )

    facts: dict[str, Any] = {
        "fecha": today.isoformat(),
        "eventos_hoy": [
            {k: e.get(k) for k in ("title", "start_time", "end_time", "location", "commitment")}
            for e in eventos
        ],
        "tareas_vencidas": [
            {"title": i.title, "vence": i.date.isoformat(), "tipo": i.kind, "asunto": i.asunto_name}
            for i in vencidas
        ],
        "tareas_hoy": [
            {"title": i.title, "asunto": i.asunto_name, "tipo": i.kind, "detalle": i.detail}
            for i in hoy
        ],
        "tareas_proximas": [
            {
                "title": i.title,
                "vence": i.date.isoformat(),
                "tipo": i.kind,
                "detalle": i.detail,
                "asunto": i.asunto_name,
            }
            for i in proximas
        ],
        "asuntos": [
            {
                "nombre": a.name,
                "prioridad": a.priority,
                "vence": a.due_on.isoformat() if a.due_on else None,
                "motivo": m,
            }
            for a, m in con_motivo
        ],
        "panorama": [a.name for a in panorama],
        "pagos_pendientes": [
            {
                "contraparte": p["counterparty"],
                "monto": p["amount"],
                "moneda": p["currency"],
                "estado": p["status"],
                "dias": p["age_days"],
            }
            for p in pagos
        ],
    }
    return [
        Draft(
            kind=KIND,
            severity="info",
            dedup_key=f"resumen:{today.isoformat()}",
            title=f"Resumen del {today:%A %d/%m}".replace("Monday", "lunes")
            .replace("Tuesday", "martes")
            .replace("Wednesday", "miércoles")
            .replace("Thursday", "jueves")
            .replace("Friday", "viernes")
            .replace("Saturday", "sábado")
            .replace("Sunday", "domingo"),
            body=body,
            facts=facts,
            llm_brief=_BRIEF,
            deep_link="/",
            expires_at=_day_bounds(today, ctx),
        )
    ]


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description=(
        "Resumen matutino: agenda de hoy, tareas, asuntos con motivo (vence o hubo novedad), "
        "pagos; lo viejo con cadencia y un panorama semanal de lo abierto."
    ),
    default_enabled=True,
    default_render="llm",
    default_config={"hora": "07:00", "dias_tareas": 7, "dias_asuntos": 7, "dia_panorama": 0},
    produce=produce,
)
