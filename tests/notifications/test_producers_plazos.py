"""Productores `asuntos_plazo` y `pagos_pendientes`: uno por asunto con plazo cercano / por pago
sin regularizar, a la hora configurada, con clave estable."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store as asuntos_store
from memex.notifications.producers import ProducerContext, all_producers, asuntos, pagos

TZ = ZoneInfo("America/Bogota")
TODAY = date(2026, 9, 10)


def _ctx(conn: Any, now: datetime, spec: Any, **overrides: Any) -> ProducerContext:
    return ProducerContext(conn, 1, now, {**spec.default_config, **overrides})


def _at(hh: int, mm: int = 0) -> datetime:
    return datetime.combine(TODAY, time(hh, mm), tzinfo=TZ)


def _pago(counterparty: str, when: datetime, status: str, amount: float = 45000) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, amount, "
                "currency, category, counterparty, place, occurred_at, occurred_at_precision, "
                "description) VALUES (1, 'egreso', :s, 'pago', :a, 'COP', 'software', :cp, '', "
                ":o, 'datetime', 'Suscripción mensual')"
            ),
            {"s": status, "a": amount, "cp": counterparty, "o": when},
        )


def test_registry_has_all_producers() -> None:
    assert set(all_producers()) == {
        "resumen_diario",
        "recordatorio_evento",
        "tareas_del_dia",
        "asuntos_plazo",
        "pagos_pendientes",
        "renovaciones",
        "verificar_cuentas",
        "sistema",
    }


@pytest.mark.asyncio
async def test_asuntos_plazo_within_window_after_hour() -> None:
    with connection() as c:
        hoy = asuntos_store.create_asunto(
            c, 1, name="Entregar informe final", kind="tramite", status="abierto", due_on=TODAY
        )
        asuntos_store.create_asunto(
            c,
            1,
            name="Matrícula",
            kind="tramite",
            status="abierto",
            due_on=TODAY + timedelta(days=1),
        )
        asuntos_store.create_asunto(
            c,
            1,
            name="Lejano",
            kind="programa",
            status="abierto",
            due_on=TODAY + timedelta(days=10),
        )
        asuntos_store.create_asunto(
            c,
            1,
            name="Vencido",
            kind="programa",
            status="abierto",
            due_on=TODAY - timedelta(days=1),
        )
        asuntos_store.create_asunto(
            c, 1, name="Cerrado", kind="tramite", status="cerrado", due_on=TODAY
        )
        asuntos_store.create_asunto(c, 1, name="Sin plazo", kind="tramite", status="abierto")
        asuntos_store.record_state(c, 1, hoy, "Falta firmar el acta", closed=False, source="llm")
    with connection() as c:
        assert await asuntos.produce(_ctx(c, _at(7, 59), asuntos.SPEC)) == []
        out = await asuntos.produce(_ctx(c, _at(8, 0), asuntos.SPEC))
        titles = sorted(d.title for d in out)
        assert titles == ["Vence hoy: Entregar informe final", "Vence mañana: Matrícula"]
        d = next(x for x in out if x.title.startswith("Vence hoy"))
        assert d.severity == "alta" and d.deep_link == f"/asuntos/{hoy}"
        assert d.dedup_key == f"asunto:{hoy}:{TODAY.isoformat()}"
        assert "Falta firmar el acta" in d.body and "(10/09)" in d.body
        assert d.expires_at == datetime.combine(TODAY, time(23, 59, 59), tzinfo=TZ)
        # Ventana configurable: con 10 días entra «Lejano».
        wide = await asuntos.produce(_ctx(c, _at(8, 0), asuntos.SPEC, dias=10))
        assert any(x.title == "Vence en 10 días: Lejano" for x in wide)


@pytest.mark.asyncio
async def test_pagos_pendientes_one_per_unresolved_payment() -> None:
    _pago("Notion Labs", datetime(2026, 9, 5, 12, tzinfo=TZ), "fallida")
    _pago("Tigo", datetime(2026, 9, 1, 12, tzinfo=TZ), "pendiente", amount=89900.5)
    _pago("Tigo", datetime(2026, 9, 3, 12, tzinfo=TZ), "completada", amount=89900.5)  # regulariza
    _pago("Uber", datetime(2026, 9, 8, 12, tzinfo=TZ), "completada")
    with connection() as c:
        assert await pagos.produce(_ctx(c, _at(7, 0), pagos.SPEC)) == []
        out = await pagos.produce(_ctx(c, _at(8, 0), pagos.SPEC))
    assert len(out) == 1
    d = out[0]
    assert d.title == "Pago fallida: Notion Labs 45,000 COP"
    assert d.dedup_key.startswith("pago:") and d.severity == "alta" and d.deep_link == "/finanzas"
    assert "05/09" in d.body and "Suscripción mensual" in d.body
    assert d.facts["counterparty"] == "Notion Labs" and d.facts["status"] == "fallida"
    assert d.expires_at == _at(8, 0) + timedelta(days=14)
    with connection() as c:
        # Edad mínima: los recientes se esperan.
        assert await pagos.produce(_ctx(c, _at(8, 0), pagos.SPEC, edad_minima_dias=3650)) == []
