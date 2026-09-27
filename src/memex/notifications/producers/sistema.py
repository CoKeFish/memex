"""Productor `sistema`: lo interno de memex (audiencia `sistema`).

Reusa el cálculo de alertas de observabilidad (`memex.core.alerts.compute_alerts`: ingesta
fallida, worker colgado o en error, saldo LLM agotado, backlog de revisión) y lo vuelve avisos. La
clave incluye el instante de la condición (`at`), así una falla NUEVA de la misma fuente genera un
aviso nuevo aunque el anterior se haya descartado, y la misma falla no se repite cada ciclo (una
alerta puede traer su propia `dedup`, p. ej. una cuenta caída: un aviso por día). Vence a las
`horas_vigencia` horas. Siempre plantilla: el detalle ya es el error literal.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from memex.core.alerts import compute_alerts
from memex.notifications.producers import Draft, ProducerContext, ProducerSpec

NAME = "sistema"
KIND_PREFIX = "sistema."


async def produce(ctx: ProducerContext) -> list[Draft]:
    vigencia = timedelta(hours=int(ctx.config["horas_vigencia"]))
    out: list[Draft] = []
    for a in compute_alerts(ctx.conn, ctx.user_id):
        at = a.get("at")
        stamp = at.isoformat() if isinstance(at, datetime) else "na"
        out.append(
            Draft(
                kind=KIND_PREFIX + str(a["kind"]),
                severity=str(a["severity"]),
                dedup_key="sistema:" + str(a.get("dedup") or f"{a['id']}:{stamp}"),
                title=str(a["title"]),
                body=str(a["detail"]),
                facts={"alert_id": a["id"], "kind": a["kind"], "at": stamp},
                deep_link=a.get("deep_link"),
                expires_at=ctx.now + vigencia,
            )
        )
    return out


SPEC = ProducerSpec(
    name=NAME,
    audience="sistema",
    description="Alertas internas: ingesta fallida, worker colgado o en error, saldo LLM, revisión",
    default_enabled=True,
    default_render="plantilla",
    default_config={"horas_vigencia": 48},
    produce=produce,
    llm_allowed=False,
)
