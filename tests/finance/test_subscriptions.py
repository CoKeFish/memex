"""Detección de suscripciones a partir de los cargos.

Los casos son PATRONES REALES tomados del historial (anonimizados a la forma del patrón), porque la
regla intuitiva «mismo monto todos los meses» se equivoca en los dos sentidos: hay suscripciones con
monto variable (facturación por uso, cobra siempre el mismo día) y recargas de saldo con monto
idéntico en fechas sueltas.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.finance import subscriptions as subs

# --- inferencia pura (sin DB) --------------------------------------------------------------- #


def test_monthly_fixed_day_with_variable_amount() -> None:
    """El caso que una regla por monto perdería: cobra SIEMPRE el día 1, importes de 5 a 37."""
    days = [date(2026, m, 1) for m in (3, 4, 5, 6)] + [date(2026, 5, 19)]  # 19/05 = cargo extra
    cadence = subs.infer_cadence(days, amount_stable=False)
    assert cadence is not None
    assert cadence.kind == "monthly" and cadence.anchor_day == 1
    assert cadence.support == 4  # abr/may/jun/mar: el extra del 19 no cuenta como patrón
    assert cadence.recurrence == ("RRULE:FREQ=MONTHLY;BYMONTHDAY=1",)
    assert "día 1" in cadence.label
    # Confianza alta pero no perfecta: 4 meses de respaldo y un cargo fuera de patrón.
    assert cadence.confidence >= 0.7


def test_same_amount_on_scattered_dates_is_not_a_subscription() -> None:
    """Recargas de saldo: importe idéntico, fechas sin patrón → NO es una renovación."""
    days = [date(2026, 4, 15), date(2026, 4, 17), date(2026, 5, 20), date(2026, 8, 21)]
    assert subs.infer_cadence(days, amount_stable=True) is None


def test_prepaid_topups_with_coincidental_day_are_rejected() -> None:
    """Recargas con días dispares: aunque un par coincida, no alcanza el respaldo mensual."""
    days = [
        date(2026, 3, 2),
        date(2026, 3, 6),
        date(2026, 4, 16),
        date(2026, 8, 6),
        date(2026, 8, 22),
    ]
    cadence = subs.infer_cadence(days)
    assert cadence is None or cadence.confidence < subs.MIN_CONFIDENCE


def test_monthly_tolerates_missing_months_with_lower_confidence() -> None:
    """La ingesta tiene huecos: faltar meses baja la confianza, no descalifica."""
    complete = subs.infer_cadence([date(2026, m, 18) for m in (2, 3, 4, 5, 6)])
    gappy = subs.infer_cadence([date(2026, 2, 18), date(2026, 5, 18), date(2026, 8, 18)])
    assert complete is not None and gappy is not None
    assert gappy.kind == "monthly" and gappy.anchor_day == 18
    assert gappy.confidence < complete.confidence


def test_end_of_month_uses_last_day_so_february_is_not_skipped() -> None:
    """Cobro el 31: con BYMONTHDAY=31 la regla saltearía febrero y la renovación se iría de mes."""
    cadence = subs.infer_cadence([date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)])
    assert cadence is not None and cadence.recurrence == ("RRULE:FREQ=MONTHLY;BYMONTHDAY=-1",)
    assert "último día" in cadence.label
    nxt = subs.next_renewal(cadence, date(2026, 1, 31), date(2026, 1, 31), today=date(2026, 2, 1))
    assert nxt == date(2026, 2, 28)  # no se salta febrero


def test_day_31_and_day_1_count_as_the_same_anchor() -> None:
    """Distancia circular: un cobro del 31 cae el 1 en los meses que no tienen 31."""
    assert subs._dom_distance(31, 1) == 1
    assert subs._dom_distance(1, 15) == 14


def test_yearly_and_weekly_cadences() -> None:
    yearly = subs.infer_cadence([date(2024, 6, 10), date(2025, 6, 11), date(2026, 6, 10)])
    assert yearly is not None and yearly.kind == "yearly"
    weekly = subs.infer_cadence([date(2026, 9, d) for d in (1, 8, 15, 22)])
    assert weekly is not None and weekly.kind == "weekly"
    biweekly = subs.infer_cadence(
        [date(2026, m, d) for m, d in ((9, 1), (9, 15), (9, 29), (10, 13))]
    )
    assert biweekly is not None and biweekly.kind == "biweekly"


def test_two_charges_a_few_days_apart_are_not_a_weekly_subscription() -> None:
    """Dos cuotas de una matrícula separadas 6 días pasaban por «suscripción semanal» e inflaban el
    compromiso mensual. Una cadencia corta necesita un ritmo, no un solo intervalo."""
    assert subs.infer_cadence([date(2026, 8, 25), date(2026, 8, 31)]) is None
    assert subs.infer_cadence([date(2026, 1, 23), date(2026, 1, 31)]) is None  # dos recargas
    assert subs.infer_cadence([date(2026, 2, 16), date(2026, 3, 4)]) is None  # "quincenal" de dos


def test_two_charges_one_month_apart_on_the_same_day_are_monthly() -> None:
    """El contrapunto: dos cobros en meses seguidos y el MISMO día sí son una renovación."""
    cadence = subs.infer_cadence([date(2026, 3, 3), date(2026, 4, 3)], amount_stable=True)
    assert cadence is not None and cadence.kind == "monthly" and cadence.anchor_day == 3


def test_a_single_charge_is_never_a_subscription() -> None:
    assert subs.infer_cadence([date(2026, 5, 1)]) is None
    assert subs.infer_cadence([]) is None


def test_next_renewal_skips_the_periods_already_missed() -> None:
    """Si se pasaron varias renovaciones sin cobro, interesa la que VIENE, no una del pasado."""
    cadence = subs.infer_cadence([date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5)])
    assert cadence is not None
    nxt = subs.next_renewal(cadence, date(2026, 1, 5), date(2026, 3, 5), today=date(2026, 6, 20))
    assert nxt == date(2026, 7, 5)


def test_amount_stats_flags_usage_based_billing() -> None:
    typical, varies = subs._amount_stats([5.47, 32.26, 37.28, 25.10])
    assert varies is True and 25 <= typical <= 35
    typical, varies = subs._amount_stats([4.99, 4.99, 4.99])
    assert varies is False and typical == 4.99


# --- detección sobre la DB ------------------------------------------------------------------- #


def _charge(conn: Any, *, counterparty: str, amount: float, on: date, currency: str = "USD") -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_finance_consolidated
                  (user_id, direction, amount, currency, category, counterparty, occurred_at,
                   occurred_at_precision, status, kind)
                VALUES (1, 'egreso', :amt, :cur, 'software', :who, :on, 'date',
                        'completada', 'pago')
                RETURNING id
                """
            ),
            {"amt": amount, "cur": currency, "who": counterparty, "on": on},
        ).scalar_one()
    )


def _seed_monthly(
    conn: Any, who: str, *, day: int = 1, months: tuple[int, ...] = (3, 4, 5, 6)
) -> None:
    for m in months:
        _charge(conn, counterparty=who, amount=10.0, on=date(2026, m, day))


def test_detect_creates_and_is_idempotent() -> None:
    with connection() as conn:
        _seed_monthly(conn, "Nubix")
        first = subs.detect(conn, 1, today=date(2026, 6, 10))
        assert first.detected == 1 and first.created == 1
        second = subs.detect(conn, 1, today=date(2026, 6, 10))
        assert second.created == 0 and second.updated == 1  # no duplica

        rows = subs.list_subscriptions(conn, 1)
        assert len(rows) == 1
        sub = rows[0]
        assert sub["provider"] == "Nubix" and sub["currency"] == "USD"
        assert sub["status"] == "activa" and sub["charges"] == 4
        assert sub["next_renewal_on"] == date(2026, 7, 1)
        assert "mensual" in sub["cadence_human"] or "MONTHLY" in str(sub["recurrence"])


def test_detect_ignores_refunds_failed_and_income() -> None:
    """Solo cuentan los pagos que de verdad salieron: un reembolso o un cobro fallido no renueva."""
    with connection() as conn:
        for m in (3, 4, 5, 6):
            conn.execute(
                text(
                    """
                    INSERT INTO mod_finance_consolidated
                      (user_id, direction, amount, currency, counterparty, occurred_at,
                       occurred_at_precision, status, kind)
                    VALUES (1, 'egreso', 9.0, 'USD', 'Reembolsos SA', :on, 'date', 'completada',
                            'reembolso')
                    """
                ),
                {"on": date(2026, m, 3)},
            )
        assert subs.detect(conn, 1, today=date(2026, 6, 10)).detected == 0


def test_charges_are_not_shared_between_subscriptions() -> None:
    with connection() as conn:
        _seed_monthly(conn, "Uno", day=4)
        _seed_monthly(conn, "Dos", day=9)
        subs.detect(conn, 1, today=date(2026, 6, 10))
        shared = conn.execute(
            text(
                "SELECT count(*) FROM mod_finance_subscription_charges "
                "GROUP BY consolidated_id HAVING count(*) > 1"
            )
        ).first()
        assert shared is None


def test_stale_subscription_is_marked_without_charges() -> None:
    """Meses sin cobro tras el patrón → `sin_cobros` (posible cancelación silenciosa)."""
    with connection() as conn:
        _seed_monthly(conn, "Firestone", day=26, months=(1, 2, 3))
        subs.detect(conn, 1, today=date(2026, 9, 12))
        assert subs.list_subscriptions(conn, 1)[0]["status"] == "sin_cobros"


def test_user_decisions_are_never_overwritten() -> None:
    """Lo que el usuario descarta no se vuelve a proponer, aunque sigan llegando cargos."""
    with connection() as conn:
        _seed_monthly(conn, "No es suscripcion", day=7)
        subs.detect(conn, 1, today=date(2026, 6, 10))
        sub_id = subs.list_subscriptions(conn, 1)[0]["id"]
        assert subs.set_status(conn, 1, sub_id, "descartada") is not None

        _charge(conn, counterparty="No es suscripcion", amount=10.0, on=date(2026, 7, 7))
        stats = subs.detect(conn, 1, today=date(2026, 7, 10))

        assert stats.skipped_user_owned == 1
        assert subs.list_subscriptions(conn, 1) == []  # descartada no aparece
        again = subs.list_subscriptions(conn, 1, include_inactive=True)
        assert again[0]["status"] == "descartada"


def test_same_provider_in_two_currencies_is_two_subscriptions() -> None:
    with connection() as conn:
        _seed_monthly(conn, "Global", day=12)
        for m in (3, 4, 5, 6):
            _charge(
                conn, counterparty="Global", amount=40000.0, on=date(2026, m, 12), currency="COP"
            )
        subs.detect(conn, 1, today=date(2026, 6, 20))
        rows = subs.list_subscriptions(conn, 1)
        assert {r["currency"] for r in rows} == {"USD", "COP"}


def test_monthly_commitment_totals_per_currency_without_mixing() -> None:
    with connection() as conn:
        _seed_monthly(conn, "Mensual", day=2)  # 10 USD/mes
        for y in (2024, 2025, 2026):
            _charge(conn, counterparty="Anual", amount=120.0, on=date(y, 5, 9))
        subs.detect(conn, 1, today=date(2026, 6, 10))
        totals = {t["currency"]: t for t in subs.monthly_commitment(conn, 1)}
        assert totals["USD"]["subscriptions"] == 2
        assert totals["USD"]["monthly"] == pytest.approx(20.0, abs=0.01)  # 10 + 120/12
