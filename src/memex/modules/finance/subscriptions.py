"""Suscripciones: deducir del historial de cargos QUÉ se repite y CUÁNDO se renueva.

Determinista, sin LLM (regla del proyecto: la herramienta según el problema). Agrupa los egresos
consolidados por proveedor + moneda y busca una cadencia. Lo aprendido de los cargos reales:

- **El indicio fuerte es el DÍA DEL MES, no el monto.** Nubix cobra siempre el día 1 con importes
  entre 5 y 37 USD (factura por uso): es una suscripción. OpenAI repite 10,00 USD exactos en fechas
  sueltas (15 abr, 17 abr, 20 may, 21 ago): son recargas de saldo, no una renovación. Una regla
  tipo «mismo monto cada mes» se equivoca en los dos sentidos.
- **La ingesta tiene huecos**, así que exigir meses consecutivos descartaría suscripciones reales.
  Se tolera que falten meses y eso baja la `confidence`, no descalifica.
- Por eso el detector PROPONE con una confianza y el usuario confirma (`pinned`) o descarta
  (`status='descartada'`, que ya no se vuelve a proponer). Nunca pisa lo que el usuario tocó.

La regla de renovación se guarda en líneas RFC 5545 y se expande con el motor del calendario
(`modules.calendar.recurrence`), que es puro: sin esto habría dos implementaciones de recurrencia.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

# Motor de recurrencia del calendario: PURO (dateutil + zona local, sin DB ni tablas de calendario).
# Se reusa a propósito para que «cada mes el día 1» signifique lo mismo en agenda y en finanzas.
from memex.modules.calendar.recurrence import RecurrenceSpec, describe, next_occurrences, to_rrule
from memex.modules.identidades.normalize import normalize_match

_log = get_logger("memex.modules.finance.subscriptions")

#: Mínimo de cargos para siquiera mirar un proveedor.
MIN_CHARGES = 2
#: Cuántos días de corrimiento se toleran respecto del día de cobro (el 1 y el 31 son "vecinos":
#: la distancia es circular, porque un cobro del 31 cae el 1 del mes siguiente en meses cortos).
ANCHOR_TOLERANCE_DAYS = 2
#: Fracción mínima que debe encajar en el día de cobro para llamarlo mensual. Se exige sobre los
#: MESES (el patrón se repite) y sobre los CARGOS (la mayoría del gasto con ese proveedor sigue el
#: patrón): sin lo segundo, unas recargas sueltas que por azar caen cerca del mismo día pasarían.
MIN_MONTHLY_RATIO = 0.6
#: Separación máxima entre dos cobros consecutivos del patrón para seguir llamándolo mensual. Tres
#: cargos del día 9 con un año de diferencia son ANUALES, no mensuales; pero la ingesta tiene
#: huecos, así que se toleran hasta ~3 meses salteados.
MAX_MONTHLY_GAP_DAYS = 130
#: Bandas de intervalo (en días) para las cadencias no mensuales.
YEARLY_BAND = (330, 400)
WEEKLY_BAND = (6, 8)
BIWEEKLY_BAND = (13, 16)
#: Cargos mínimos para aceptar una cadencia CORTA (semanal/quincenal). Con solo dos, un intervalo
#: dentro de la banda no dice nada: dos cuotas de una matrícula separadas 6 días, o dos recargas de
#: saldo, pasarían por "semanal". Con cuatro hay tres intervalos seguidos, o sea un ritmo. Lo anual
#: se queda en dos: que dos cobros caigan a ~365 días por casualidad es muy improbable.
MIN_CHARGES_SHORT_CADENCE = 4
#: Dispersión relativa del monto a partir de la cual se considera que cobra por uso.
AMOUNT_VARIES_SPREAD = 0.25
#: Confianza mínima para proponer la suscripción.
MIN_CONFIDENCE = 0.5
#: Cuántos periodos puede pasar sin cobro antes de marcarla `sin_cobros`.
STALE_PERIODS = 2
#: Días nominales de cada cadencia (para vencimiento y totales mensuales).
_PERIOD_DAYS = {"monthly": 31, "yearly": 366, "weekly": 7, "biweekly": 14}


@dataclass(frozen=True)
class Cadence:
    """Cadencia inferida de una serie de fechas de cobro."""

    #: `monthly` | `yearly` | `weekly` | `biweekly`.
    kind: str
    #: Día del mes en que cobra (solo `monthly`); None en las demás.
    anchor_day: int | None
    #: Cuántos periodos distintos respaldan el patrón.
    support: int
    confidence: float
    #: Líneas RFC 5545, listas para `next_occurrences`.
    recurrence: tuple[str, ...]
    #: Texto legible («mensual, el día 1»).
    label: str

    @property
    def period_days(self) -> int:
        return _PERIOD_DAYS[self.kind]


def _dom_distance(a: int, b: int) -> int:
    """Distancia CIRCULAR entre días del mes: el 31 y el 1 están a 1, no a 30 (un cobro del 31 cae
    el 1 en meses de 30 y en febrero)."""
    d = abs(a - b)
    return min(d, 31 - d)


def _months(days: Sequence[date]) -> set[tuple[int, int]]:
    return {(d.year, d.month) for d in days}


def _aligned(days: Sequence[date], anchor: int) -> list[date]:
    """Cargos que caen en el día de cobro (con tolerancia)."""
    return sorted(d for d in days if _dom_distance(d.day, anchor) <= ANCHOR_TOLERANCE_DAYS)


def _best_anchor(days: Sequence[date]) -> tuple[int, int]:
    """Día de cobro más respaldado y en cuántos MESES distintos cae (contar meses, no cargos, evita
    que dos cobros del mismo mes inflen el patrón)."""
    best_day, best_support = 0, 0
    for candidate in sorted({d.day for d in days}):
        support = len(_months(_aligned(days, candidate)))
        if support > best_support:
            best_day, best_support = candidate, support
    return best_day, best_support


def _intervals(days: Sequence[date]) -> list[int]:
    ordered = sorted(days)
    return [(b - a).days for a, b in pairwise(ordered)]


def _confidence(ratio: float, support: int, amount_stable: bool) -> float:
    """Qué tan creíble es el patrón: cuánto del historial encaja (`ratio`) y cuánta repetición hay
    (`support`), con un empujón si además el monto es estable. Sin decimales de más: es una señal
    para ordenar y decidir, no una probabilidad."""
    count_factor = min(support, 6) / 6
    score = 0.55 * ratio + 0.45 * count_factor + (0.05 if amount_stable else 0.0)
    return round(min(score, 0.99), 2)


def infer_cadence(days: Sequence[date], *, amount_stable: bool = False) -> Cadence | None:
    """Cadencia de una serie de fechas de cobro, o None si no hay patrón (cargos sueltos).

    Prioriza el día del mes; si no hay, prueba las bandas anual/quincenal/semanal por intervalo.
    """
    unique = sorted(set(days))
    if len(unique) < MIN_CHARGES:
        return None

    # 1. Mensual: la mayoría de los meses con cargo caen en el mismo día. Cuatro condiciones, cada
    #    una para descartar un falso positivo visto en datos reales:
    #      a) al menos 2 meses distintos    — varios cargos de un mismo mes no prueban nada mensual;
    #      b) la mayoría de los MESES encaja — el patrón se repite;
    #      c) la mayoría de los CARGOS encaja — si casi todo el gasto queda fuera del patrón, son
    #         compras sueltas que por azar rozaron el día (recargas de saldo);
    #      d) 3 meses de respaldo, o un encaje perfecto con 2 — dos coincidencias sobre tres meses
    #         es ruido, pero dos cargos que caen los dos en el mismo día ya dicen algo.
    anchor, support = _best_anchor(unique)
    total_months = len(_months(unique))
    aligned = _aligned(unique, anchor)
    months_ratio = support / total_months if total_months else 0.0
    charges_ratio = len(aligned) / len(unique)
    gaps_aligned = _intervals(aligned)
    monthly_rhythm = not gaps_aligned or statistics.median(gaps_aligned) <= MAX_MONTHLY_GAP_DAYS
    if (
        support >= MIN_CHARGES
        and months_ratio >= MIN_MONTHLY_RATIO
        and charges_ratio >= MIN_MONTHLY_RATIO
        and (support >= 3 or months_ratio >= 0.999)
        and monthly_rhythm
    ):
        # Día 29-31: en los meses que no lo tienen, el cobro cae el último día. `BYMONTHDAY=-1` es
        # la lectura útil; con `BYMONTHDAY=31` la regla SALTEA febrero y la renovación se iría al
        # mes siguiente.
        monthday = -1 if anchor >= 29 else anchor
        spec = RecurrenceSpec(freq="monthly", byday=())
        rrule = to_rrule(spec) + f";BYMONTHDAY={monthday}"
        label = describe_rule((rrule,))
        return Cadence(
            kind="monthly",
            anchor_day=anchor,
            support=support,
            confidence=_confidence(min(months_ratio, charges_ratio), support, amount_stable),
            recurrence=(rrule,),
            label=label,
        )

    # 2. Cadencias por intervalo: TODOS los intervalos deben caer en la banda (un solo cargo fuera
    #    de ritmo ya delata que son compras sueltas, no una renovación).
    gaps = _intervals(unique)
    if not gaps:
        return None
    median = statistics.median(gaps)
    for kind, (lo, hi), spec, min_charges in (
        ("yearly", YEARLY_BAND, RecurrenceSpec(freq="yearly"), MIN_CHARGES),
        (
            "biweekly",
            BIWEEKLY_BAND,
            RecurrenceSpec(freq="weekly", interval=2),
            MIN_CHARGES_SHORT_CADENCE,
        ),
        ("weekly", WEEKLY_BAND, RecurrenceSpec(freq="weekly"), MIN_CHARGES_SHORT_CADENCE),
    ):
        if len(unique) >= min_charges and lo <= median <= hi and all(lo <= g <= hi for g in gaps):
            support = len(unique)
            return Cadence(
                kind=kind,
                anchor_day=None,
                support=support,
                confidence=_confidence(1.0, support, amount_stable),
                recurrence=(to_rrule(spec),),
                label=describe([to_rrule(spec)]),
            )
    return None


def describe_rule(recurrence: Sequence[str]) -> str:
    """Texto legible de la regla, con el día de cobro incluido. `describe` del calendario no muestra
    `BYMONTHDAY` (allí la fecha la lleva el evento), pero acá el día ES la respuesta a «¿cuándo se
    renueva?»."""
    if not recurrence:
        return ""
    base = describe(list(recurrence))
    rule = " ".join(str(line) for line in recurrence).upper()
    if "BYMONTHDAY=-1" in rule:
        return f"{base}, el último día del mes"
    match = re.search(r"BYMONTHDAY=(\d+)", rule)
    return f"{base}, el día {int(match.group(1))}" if match else base


def _amount_stats(amounts: Sequence[float]) -> tuple[float, bool]:
    """(monto típico, ¿varía?). La mediana aguanta un cargo atípico mejor que el promedio."""
    typical = float(statistics.median(amounts))
    if typical <= 0:
        return typical, False
    spread = (max(amounts) - min(amounts)) / typical
    return typical, spread > AMOUNT_VARIES_SPREAD


def next_renewal(
    cadence: Cadence, first_charge: date, last_charge: date, *, today: date
) -> date | None:
    """Próxima renovación: la siguiente ocurrencia de la regla DESPUÉS del último cobro, nunca en el
    pasado (si se saltó varias, interesa la que viene)."""
    after = max(last_charge + timedelta(days=1), today)
    upcoming = next_occurrences(first_charge, list(cadence.recurrence), after=after, limit=1)
    return upcoming[0] if upcoming else None


@dataclass
class DetectStats:
    """Resumen de una corrida del detector."""

    providers: int = 0  # grupos proveedor+moneda mirados
    detected: int = 0  # con cadencia reconocida
    created: int = 0
    updated: int = 0
    skipped_user_owned: int = 0  # fijadas o descartadas por el usuario: no se tocan


def _charge_rows(conn: Connection, user_id: int) -> list[dict[str, Any]]:
    """Egresos consolidados que pueden formar parte de una suscripción: plata que SALIÓ de verdad
    (`completada`), pagos (un reembolso no es una renovación)."""
    return [
        dict(r)
        for r in conn.execute(
            text(
                """
                SELECT id, counterparty, counterparty_identity_id, amount, currency, category,
                       occurred_at::date AS charged_on
                FROM mod_finance_consolidated
                WHERE user_id = :uid AND NOT deleted AND direction = 'egreso'
                  AND status = 'completada' AND kind = 'pago'
                  AND counterparty IS NOT NULL AND counterparty <> ''
                ORDER BY occurred_at
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    ]


def _provider_key(row: dict[str, Any]) -> str:
    """Clave natural del proveedor: la identidad del directorio si la hay (absorbe las variantes de
    nombre ya deduplicadas), si no el nombre normalizado."""
    ident = row.get("counterparty_identity_id")
    if ident is not None:
        return f"ident:{int(ident)}"
    return "name:" + normalize_match(str(row["counterparty"]))


def detect(conn: Connection, user_id: int, *, today: date | None = None) -> DetectStats:
    """Recorre el historial de cargos y propone/actualiza suscripciones. Idempotente: reconoce por
    `(provider_key, currency)`. No toca las `pinned` (el usuario mandó) ni las `descartada`."""
    today = today or date.today()
    stats = DetectStats()

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _charge_rows(conn, user_id):
        groups.setdefault((_provider_key(row), str(row["currency"])), []).append(row)

    owned = {
        (str(r["provider_key"]), str(r["currency"]))
        for r in conn.execute(
            text(
                "SELECT provider_key, currency FROM mod_finance_subscriptions "
                "WHERE user_id = :uid AND (pinned OR status = 'descartada')"
            ),
            {"uid": user_id},
        ).mappings()
    }

    for (key, currency), rows in sorted(groups.items()):
        if len(rows) < MIN_CHARGES:
            continue
        stats.providers += 1
        if (key, currency) in owned:
            stats.skipped_user_owned += 1
            continue
        days = [r["charged_on"] for r in rows]
        amounts = [float(r["amount"]) for r in rows]
        typical, varies = _amount_stats(amounts)
        cadence = infer_cadence(days, amount_stable=not varies)
        if cadence is None or cadence.confidence < MIN_CONFIDENCE:
            continue
        stats.detected += 1
        first, last = min(days), max(days)
        renewal = next_renewal(cadence, first, last, today=today)
        stale = (today - last).days > STALE_PERIODS * cadence.period_days
        # El nombre más largo suele ser el más informativo ("Nubix Holdings" > "Nubix").
        provider = max((str(r["counterparty"]) for r in rows), key=len)
        identity = next(
            (r["counterparty_identity_id"] for r in rows if r["counterparty_identity_id"]), None
        )
        evidence = (
            f"{len(rows)} cargos entre {first.isoformat()} y {last.isoformat()}; {cadence.label}"
            + ("; el monto varía (cobra por uso)" if varies else "")
        )
        created = _upsert(
            conn,
            user_id,
            key=key,
            currency=currency,
            provider=provider,
            identity_id=int(identity) if identity is not None else None,
            typical=typical,
            varies=varies,
            cadence=cadence,
            first=first,
            last=last,
            renewal=renewal,
            status="sin_cobros" if stale else "activa",
            evidence=evidence,
            charge_ids=[int(r["id"]) for r in rows],
        )
        if created:
            stats.created += 1
        else:
            stats.updated += 1

    _log.info(
        "finance.subscriptions.detect",
        user_id=user_id,
        providers=stats.providers,
        detected=stats.detected,
        created=stats.created,
        updated=stats.updated,
    )
    return stats


def _upsert(
    conn: Connection,
    user_id: int,
    *,
    key: str,
    currency: str,
    provider: str,
    identity_id: int | None,
    typical: float,
    varies: bool,
    cadence: Cadence,
    first: date,
    last: date,
    renewal: date | None,
    status: str,
    evidence: str,
    charge_ids: Sequence[int],
) -> bool:
    """Inserta o actualiza la suscripción y reemplaza sus cargos de evidencia. True si la creó."""
    row = conn.execute(
        text(
            """
            INSERT INTO mod_finance_subscriptions
              (user_id, provider, provider_identity_id, provider_key, currency, amount_typical,
               amount_varies, recurrence, first_charge_on, last_charge_on, next_renewal_on,
               status, source, confidence, charges, evidence)
            VALUES (:uid, :provider, :ident, :key, :cur, :amount, :varies, CAST(:rec AS TEXT[]),
                    :first, :last, :renewal, :status, 'inferida', :conf, :charges, :evidence)
            ON CONFLICT (user_id, provider_key, currency) DO UPDATE SET
              provider = EXCLUDED.provider,
              provider_identity_id = EXCLUDED.provider_identity_id,
              amount_typical = EXCLUDED.amount_typical,
              amount_varies = EXCLUDED.amount_varies,
              recurrence = EXCLUDED.recurrence,
              first_charge_on = EXCLUDED.first_charge_on,
              last_charge_on = EXCLUDED.last_charge_on,
              next_renewal_on = EXCLUDED.next_renewal_on,
              -- Una cancelación declarada manda sobre lo que digan los cargos.
              status = CASE WHEN mod_finance_subscriptions.status = 'cancelada' THEN 'cancelada'
                            ELSE EXCLUDED.status END,
              confidence = EXCLUDED.confidence,
              charges = EXCLUDED.charges,
              evidence = EXCLUDED.evidence,
              updated_at = NOW()
            RETURNING id, (xmax = 0) AS inserted
            """
        ),
        {
            "uid": user_id,
            "provider": provider,
            "ident": identity_id,
            "key": key,
            "cur": currency,
            "amount": typical,
            "varies": varies,
            "rec": list(cadence.recurrence),
            "first": first,
            "last": last,
            "renewal": renewal,
            "status": status,
            "conf": cadence.confidence,
            "charges": len(charge_ids),
            "evidence": evidence,
        },
    ).one()
    sub_id = int(row[0])
    # La evidencia se reemplaza entera: si un cargo dejó de pertenecer (se consolidó distinto), no
    # queda colgado. El UNIQUE(consolidated_id) impide que dos suscripciones se peleen un cargo.
    conn.execute(
        text("DELETE FROM mod_finance_subscription_charges WHERE subscription_id = :sid"),
        {"sid": sub_id},
    )
    for cid in charge_ids:
        conn.execute(
            text(
                "INSERT INTO mod_finance_subscription_charges (subscription_id, consolidated_id) "
                "VALUES (:sid, :cid) ON CONFLICT (consolidated_id) DO NOTHING"
            ),
            {"sid": sub_id, "cid": cid},
        )
    return bool(row[1])


def list_subscriptions(
    conn: Connection,
    user_id: int,
    *,
    include_inactive: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Suscripciones ordenadas por la próxima renovación (lo que se viene primero). Por default
    oculta las canceladas y descartadas."""
    rows = (
        conn.execute(
            text(
                """
                SELECT s.id, s.provider, s.provider_identity_id, s.currency, s.amount_typical,
                       s.amount_varies, s.recurrence, s.first_charge_on, s.last_charge_on,
                       s.next_renewal_on, s.status, s.source, s.confidence, s.charges, s.evidence,
                       s.notes, s.pinned,
                       (s.next_renewal_on - CURRENT_DATE) AS days_until
                FROM mod_finance_subscriptions s
                WHERE s.user_id = :uid
                  AND (:all OR s.status IN ('activa', 'sin_cobros'))
                ORDER BY s.next_renewal_on NULLS LAST, s.provider
                LIMIT :limit
                """
            ),
            {"uid": user_id, "all": include_inactive, "limit": limit},
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["amount_typical"] = (
            float(d["amount_typical"]) if d["amount_typical"] is not None else None
        )
        d["recurrence"] = list(d["recurrence"] or [])
        d["cadence_human"] = describe_rule(d["recurrence"])
        d["days_until"] = int(d["days_until"]) if d["days_until"] is not None else None
        out.append(d)
    return out


def monthly_commitment(conn: Connection, user_id: int) -> list[dict[str, Any]]:
    """Cuánto compromete al mes cada moneda: el costo de las suscripciones activas normalizado a
    un mes (lo anual se divide por 12, lo semanal se multiplica por ~4,33). No convierte monedas:
    mezclar USD y COP con una tasa aproximada daría un número que parece exacto y no lo es."""
    subs = list_subscriptions(conn, user_id, limit=1000)
    per_currency: dict[str, dict[str, Any]] = {}
    for s in subs:
        if s["status"] != "activa" or s["amount_typical"] is None or not s["recurrence"]:
            continue
        rule = " ".join(s["recurrence"]).upper()
        if "FREQ=MONTHLY" in rule:
            factor = 1.0
        elif "FREQ=YEARLY" in rule:
            factor = 1 / 12
        elif "FREQ=WEEKLY" in rule:
            factor = 52 / 12 / (2 if "INTERVAL=2" in rule else 1)
        else:
            continue
        slot = per_currency.setdefault(
            s["currency"], {"currency": s["currency"], "monthly": 0.0, "subscriptions": 0}
        )
        slot["monthly"] += s["amount_typical"] * factor
        slot["subscriptions"] += 1
    for slot in per_currency.values():
        slot["monthly"] = round(slot["monthly"], 2)
    return sorted(per_currency.values(), key=lambda s: -s["monthly"])


def set_status(
    conn: Connection, user_id: int, subscription_id: int, status: str
) -> dict[str, Any] | None:
    """Cambia el estado a mano (cancelada / descartada / activa) y lo FIJA: el detector deja de
    pisarlo. Devuelve la fila o None si no existe."""
    row = conn.execute(
        text(
            "UPDATE mod_finance_subscriptions SET status = :st, pinned = TRUE, updated_at = NOW() "
            "WHERE id = :id AND user_id = :uid RETURNING id, provider, status"
        ),
        {"st": status, "id": subscription_id, "uid": user_id},
    ).first()
    return None if row is None else {"id": int(row[0]), "provider": row[1], "status": row[2]}
