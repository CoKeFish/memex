"""Productor `renovaciones`: avisa antes de que se cobre una suscripción.

Sirve para llegar a tiempo a cancelar lo que ya no se usa o a tener saldo. Mira
`mod_finance_subscriptions.next_renewal_on` dentro de una ventana de `dias_anticipacion` días.

Solo avisa de las `activa`: una `sin_cobros` ya no cobra (y avisar de un cobro que no va a llegar es
ruido), y las canceladas o descartadas no existen para esto. Idempotente POR RENOVACIÓN
(`dedup_key` lleva la fecha), así el mes que viene vuelve a avisar pero hoy no repite. Siempre
plantilla: el dato es la fecha y el monto, no hay nada que redactar.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text

from memex.notifications.producers import Draft, ProducerContext, ProducerSpec

NAME = "renovaciones"
KIND = "finanzas.renovacion"


def _money(row: dict[str, Any]) -> str:
    if row["amount_typical"] is None:
        return ""
    amount = f"{float(row['amount_typical']):,.2f} {row['currency']}"
    # Facturación por uso: el monto es una referencia, no una promesa. Decirlo evita la sorpresa.
    return f"~{amount} (varía)" if row["amount_varies"] else amount


async def produce(ctx: ProducerContext) -> list[Draft]:
    lead = int(ctx.config["dias_anticipacion"])
    today = ctx.now.date()
    rows = (
        ctx.conn.execute(
            text(
                """
                SELECT id, provider, amount_typical, amount_varies, currency, next_renewal_on
                FROM mod_finance_subscriptions
                WHERE user_id = :uid AND status = 'activa' AND next_renewal_on IS NOT NULL
                  AND next_renewal_on BETWEEN :today AND :until
                ORDER BY next_renewal_on, provider
                """
            ),
            {"uid": ctx.user_id, "today": today, "until": today + timedelta(days=lead)},
        )
        .mappings()
        .all()
    )

    drafts: list[Draft] = []
    for r in rows:
        row = dict(r)
        renewal = row["next_renewal_on"]
        days = (renewal - today).days
        when = "hoy" if days == 0 else ("mañana" if days == 1 else f"en {days} días")
        money = _money(row)
        drafts.append(
            Draft(
                kind=KIND,
                severity="info",
                # La fecha entra en la clave: cada renovación avisa una vez.
                dedup_key=f"renovacion:{row['id']}:{renewal.isoformat()}",
                title=f"Se renueva {row['provider']} {when}",
                body=(
                    f"{row['provider']} se renueva el {renewal.isoformat()}"
                    + (f" por {money}" if money else "")
                    + "."
                ),
                facts={
                    "subscription_id": row["id"],
                    "provider": row["provider"],
                    "renews_on": renewal.isoformat(),
                    "days_until": days,
                    "amount": float(row["amount_typical"]) if row["amount_typical"] else None,
                    "currency": row["currency"],
                    "amount_varies": bool(row["amount_varies"]),
                },
                deep_link="/finanzas",
                # Vence con el cobro: después de renovar, avisarlo ya no sirve de nada.
                expires_at=ctx.now + timedelta(days=max(days, 0) + 1),
            )
        )
    return drafts


SPEC = ProducerSpec(
    name=NAME,
    audience="personal",
    description="Avisa antes de que se cobre una suscripción (a tiempo para cancelarla)",
    default_enabled=True,
    default_render="plantilla",
    default_config={"dias_anticipacion": 3},
    produce=produce,
    llm_allowed=False,
)
