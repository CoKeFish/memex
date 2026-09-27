"""Productor `pagos_pendientes`: un aviso por pago fallido o pendiente sin regularizar.

Lee la vista determinista de finanzas (`pending_payments`: egresos `fallida`/`pendiente` sin un
pago `completada` posterior a la misma contraparte) y, a la hora configurada, emite UN aviso por
transacción (`pago:<id>`). Cuando finanzas registra el pago, el pendiente desaparece de la vista y
no vuelve a avisarse; el aviso ya emitido vence a los `dias_vigencia` días. Severidad alta.
Siempre plantilla. Decisión del dueño (2026-09-10): «los pagos viven en finanzas»; esto solo señala.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from memex.modules.finance.manual import pending_payments
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec
from memex.notifications.settings import parse_hhmm

NAME = "pagos_pendientes"
KIND = "finance.pago_pendiente"


def _monto(p: dict[str, Any]) -> str:
    amount = float(p["amount"])
    shown = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    return f"{shown} {p['currency']}"


async def produce(ctx: ProducerContext) -> list[Draft]:
    hora = parse_hhmm(str(ctx.config["hora"]))
    if ctx.now.time() < hora:
        return []
    min_age = int(ctx.config["edad_minima_dias"])
    vigencia = timedelta(days=int(ctx.config["dias_vigencia"]))
    out: list[Draft] = []
    for p in pending_payments(ctx.conn, ctx.user_id, limit=100):
        if int(p["age_days"]) < min_age:
            continue
        who = p["counterparty"] or "sin contraparte"
        when = p["occurred_at"]
        fecha = when.strftime("%d/%m") if hasattr(when, "strftime") else str(when)[:10]
        parts = [f"{fecha} · {p['age_days']} días sin regularizar"]
        if p.get("description"):
            parts.append(str(p["description"]))
        out.append(
            Draft(
                kind=KIND,
                severity="alta",
                dedup_key=f"pago:{p['id']}",
                title=f"Pago {p['status']}: {who} {_monto(p)}",
                body=" · ".join(parts),
                facts={
                    "transaction_id": p["id"],
                    "counterparty": p["counterparty"],
                    "amount": p["amount"],
                    "currency": p["currency"],
                    "status": p["status"],
                    "age_days": p["age_days"],
                },
                deep_link="/finanzas",
                expires_at=ctx.now + vigencia,
            )
        )
    return out


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description="Un aviso por pago fallido o pendiente que finanzas aún no ve regularizado.",
    default_enabled=True,
    default_render="plantilla",
    default_config={"hora": "08:00", "edad_minima_dias": 0, "dias_vigencia": 14},
    produce=produce,
    llm_allowed=False,
)
