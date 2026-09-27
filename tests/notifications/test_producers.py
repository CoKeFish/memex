"""Productores: qué avisan y cuándo (hora configurada, anticipación, idempotencia por clave)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store as asuntos_store
from memex.modules.calendar.manual import add_event
from memex.modules.tareas.module import add as add_tarea
from memex.notifications.producers import (
    ProducerContext,
    all_producers,
    eventos,
    renovaciones,
    resumen,
    sistema,
    tareas,
    verificar_cuentas,
)

TZ = ZoneInfo("America/Bogota")
TODAY = date(2026, 9, 10)


def _ctx(conn: Any, now: datetime, spec: Any, **overrides: Any) -> ProducerContext:
    return ProducerContext(conn, 1, now, {**spec.default_config, **overrides})


def _at(hh: int, mm: int = 0) -> datetime:
    return datetime.combine(TODAY, time(hh, mm), tzinfo=TZ)


def test_registry_lists_the_producers() -> None:
    names = set(all_producers())
    assert {
        "resumen_diario",
        "recordatorio_evento",
        "tareas_del_dia",
        "verificar_cuentas",
        "sistema",
    } <= names
    assert all_producers()["verificar_cuentas"].audience == "sistema"
    assert all_producers()["recordatorio_evento"].llm_allowed is False
    assert all_producers()["resumen_diario"].default_render == "llm"


def test_recordatorio_evento_within_lead_only() -> None:
    add_event(
        1,
        title="Clase de inglés",
        starts_on=TODAY,
        start_time=time(10, 0),
        end_time=time(12, 0),
        location="Britano",
    )
    add_event(1, title="Todo el día", starts_on=TODAY)  # sin hora: lo cubre el resumen
    with connection() as c:
        early = asyncio.run(eventos.produce(_ctx(c, _at(8, 30), eventos.SPEC)))
        assert early == []
        due = asyncio.run(eventos.produce(_ctx(c, _at(9, 15), eventos.SPEC)))
        assert len(due) == 1
        d = due[0]
        assert d.title == "En 45 min: Clase de inglés"
        assert d.body == "10:00-12:00 · Britano"
        assert d.dedup_key.startswith("evento:") and d.dedup_key.endswith(f":{TODAY.isoformat()}")
        assert d.expires_at == _at(10, 0)
        assert d.deep_link == "/calendario"
        # Anticipación configurable: con 30 min, a las 9:15 todavía no.
        assert (
            asyncio.run(eventos.produce(_ctx(c, _at(9, 15), eventos.SPEC, anticipacion_min=30)))
            == []
        )
        # Ya empezó → no se recuerda.
        assert asyncio.run(eventos.produce(_ctx(c, _at(10, 1), eventos.SPEC))) == []


@pytest.mark.asyncio
async def test_tareas_del_dia_fires_after_hour_for_due_today_only() -> None:
    with connection() as c:
        add_tarea(c, 1, title="Entregar informe", due_on=TODAY, priority="alta")
        add_tarea(c, 1, title="Mañana", due_on=TODAY + timedelta(days=1))
        add_tarea(c, 1, title="Vencida ayer", due_on=TODAY - timedelta(days=1))
        add_tarea(c, 1, title="Sin fecha")
    with connection() as c:
        assert await tareas.produce(_ctx(c, _at(7, 59), tareas.SPEC)) == []
        out = await tareas.produce(_ctx(c, _at(8, 0), tareas.SPEC))
        assert [d.title for d in out] == ["Vence hoy: Entregar informe"]
        assert out[0].severity == "alta" and out[0].deep_link == "/tareas"
        assert "Prioridad alta" in out[0].body
        # Hora configurable.
        assert await tareas.produce(_ctx(c, _at(8, 0), tareas.SPEC, hora="09:00")) == []


def test_resumen_diario_sections_and_facts() -> None:
    add_event(1, title="Clase", starts_on=TODAY, start_time=time(10, 0))
    with connection() as c:
        add_tarea(c, 1, title="Pagar matrícula", due_on=TODAY - timedelta(days=2))
        add_tarea(c, 1, title="Leer paper", due_on=TODAY + timedelta(days=3))
        asuntos_store.create_asunto(
            c, 1, name="Condonación FONDEX", kind="tramite", status="abierto", priority="alta"
        )
        asuntos_store.create_asunto(
            c,
            1,
            name="Curso B1",
            kind="programa",
            status="abierto",
            due_on=TODAY + timedelta(days=2),
        )
        asuntos_store.create_asunto(
            c,
            1,
            name="Lejano",
            kind="programa",
            status="abierto",
            due_on=TODAY + timedelta(days=40),
        )
        c.execute(
            text(
                "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, amount, "
                "currency, category, counterparty, place, occurred_at, occurred_at_precision, "
                "description) VALUES (1, 'egreso', 'fallida', 'pago', 45000, 'COP', 'software', "
                "'Notion Labs', '', :o, 'datetime', '')"
            ),
            # la edad del pago se mide contra la fecha REAL (CURRENT_DATE): 5 días = fuera de
            # la cadencia
            {"o": datetime.now(TZ) - timedelta(days=5)},
        )
    with connection() as c:
        assert asyncio.run(resumen.produce(_ctx(c, _at(6, 59), resumen.SPEC))) == []
        out = asyncio.run(resumen.produce(_ctx(c, _at(7, 0), resumen.SPEC)))
    assert len(out) == 1
    d = out[0]
    assert d.dedup_key == "resumen:2026-09-10"
    assert d.title.startswith("Resumen del jueves")
    assert "Hoy:\n• 10:00 Clase" in d.body
    assert "Tareas vencidas:\n• Pagar matrícula (vencida el 08/09)" in d.body
    assert "Próximas tareas:\n• Leer paper (13/09)" in d.body
    # prioridad alta sin motivo (sin plazo, sin novedad) NO sale a diario: es jueves, no hay
    # panorama; la condonación no se repite cada mañana
    assert "Condonación" not in d.body
    assert "• Curso B1 [normal] · vence 12/09" in d.body
    assert "Lejano" not in d.body
    # el pago fallido lleva 5 días: fuera de la cadencia (0,1,2,3,7,14…) no se repite
    assert "Pagos sin regularizar" not in d.body
    assert d.llm_brief and d.facts["fecha"] == "2026-09-10"
    assert [a["nombre"] for a in d.facts["asuntos"]] == ["Curso B1"]
    assert d.facts["asuntos"][0]["motivo"] == "vence 12/09"
    assert d.facts["pagos_pendientes"] == [] and d.facts["panorama"] == []
    assert d.expires_at == datetime.combine(TODAY, time(23, 59, 59), tzinfo=TZ)


@pytest.mark.asyncio
async def test_resumen_diario_empty_day() -> None:
    with connection() as c:
        out = await resumen.produce(_ctx(c, _at(7, 30), resumen.SPEC))
    assert out[0].body.startswith("Nada pendiente")


def test_en_cadencia() -> None:
    assert all(resumen.en_cadencia(d) for d in (0, 1, 2, 3, 7, 14, 21, 30, 60, 90))
    assert not any(resumen.en_cadencia(d) for d in (4, 5, 6, 8, 13, 15, 29, 31, 45))


def test_resumen_novedad_y_cadencia_de_lo_viejo() -> None:
    """Un asunto de prioridad alta sale cuando hubo NOVEDAD (cambió su estado desde ayer); una
    tarea vencida hace 5 días no se repite, hace 7 sí; un pago fallido igual."""
    with connection() as c:
        cond = asuntos_store.create_asunto(
            c, 1, name="Condonación FONDEX", kind="tramite", status="abierto", priority="alta"
        )
        asuntos_store.record_state(c, 1, cond, "FONDEX pidió el certificado", closed=False)
        asuntos_store.create_asunto(
            c, 1, name="Pasaporte", kind="tramite", status="abierto", priority="alta"
        )
        add_tarea(c, 1, title="Vencida hace 5", due_on=TODAY - timedelta(days=5))
        add_tarea(c, 1, title="Vencida hace 7", due_on=TODAY - timedelta(days=7))
        for dias, who in ((5, "Nubix"), (7, "Notion Labs")):
            c.execute(
                text(
                    "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, "
                    "amount, currency, category, counterparty, place, occurred_at, "
                    "occurred_at_precision, description) VALUES (1, 'egreso', 'fallida', 'pago', "
                    "100, 'USD', 'software', :w, '', :o, 'datetime', '')"
                ),
                # edad medida contra la fecha REAL (CURRENT_DATE), no contra TODAY del test
                {"w": who, "o": datetime.now(TZ) - timedelta(days=dias)},
            )
    with connection() as c:
        (d,) = asyncio.run(resumen.produce(_ctx(c, _at(7, 0), resumen.SPEC)))
    assert "• Condonación FONDEX [alta] · novedad: FONDEX pidió el certificado" in d.body
    assert "Pasaporte" not in d.body  # alta sin novedad ni plazo: no a diario
    assert "Vencida hace 7" in d.body and "Vencida hace 5" not in d.body
    assert "Notion Labs" in d.body and "Nubix" not in d.body
    # el lunes (panorama) sale TODO lo abierto en alta y todo lo vencido
    lunes = datetime.combine(date(2026, 9, 14), time(7, 0), tzinfo=TZ)
    with connection() as c:
        (p,) = asyncio.run(resumen.produce(_ctx(c, lunes, resumen.SPEC)))
    assert "Panorama de la semana (prioridad alta): Pasaporte" in p.body
    assert "Vencida hace 9" not in p.body  # (no existe: solo comprueba que lo vencido está)
    assert "Vencida hace 5" in p.body and "Nubix" in p.body
    assert p.facts["panorama"] == ["Pasaporte"]
    # el panorama es configurable (jueves → hoy)
    with connection() as c:
        (j,) = asyncio.run(resumen.produce(_ctx(c, _at(7, 0), resumen.SPEC, dia_panorama=3)))
    assert "Panorama de la semana" in j.body


@pytest.mark.asyncio
async def test_sistema_maps_observability_alerts() -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO worker_runs (user_id, job, status, started_at, finished_at, error) "
                "VALUES (1, 'extract', 'error', :s, :f, 'insufficient balance (402)')"
            ),
            {"s": datetime(2026, 9, 10, 5, tzinfo=TZ), "f": datetime(2026, 9, 10, 5, 1, tzinfo=TZ)},
        )
    with connection() as c:
        out = await sistema.produce(_ctx(c, _at(9), sistema.SPEC))
    assert len(out) == 1
    d = out[0]
    assert d.kind == "sistema.saldo" and d.severity == "critica"
    assert d.title == "Saldo/cuota LLM agotado"
    assert d.dedup_key.startswith("sistema:worker-extract:2026-09-10")
    assert d.deep_link == "/pipeline"
    assert d.expires_at == _at(9) + timedelta(hours=48)
    assert sistema.SPEC.audience == "sistema"


def _account(
    conn: Any, alias: str, *, status: str, checked_at: datetime | None, code: str | None = None
) -> int:
    aid = int(
        conn.execute(
            text(
                "INSERT INTO accounts (user_id, alias, provider, kind, health_status, "
                "last_health_check_at, health_code, health_detail) "
                "VALUES (1, :a, 'google', 'email', :s, :t, :c, :d) RETURNING id"
            ),
            {"a": alias, "s": status, "t": checked_at, "c": code, "d": "el acceso caducó"},
        ).scalar_one()
    )
    conn.execute(
        text("INSERT INTO sources (user_id, name, type, account_id) VALUES (1, :n, 'imap', :a)"),
        {"n": f"src-{alias}", "a": aid},
    )
    return aid


def test_sistema_reports_unhealthy_account_once_per_day() -> None:
    """Una cuenta cuyas credenciales fallaron es una alerta de sistema; la clave de dedup lleva el
    DÍA (no el instante del chequeo), así el daemon puede revalidar cada pocas horas sin repetir el
    aviso; al día siguiente, si sigue caída, avisa de nuevo."""
    with connection() as c:
        aid = _account(
            c, "gmail-caida", status="unhealthy", checked_at=_at(9), code="reauth_required"
        )
        _account(c, "gmail-sana", status="healthy", checked_at=_at(9))
        drafts = asyncio.run(sistema.produce(_ctx(c, _at(9, 5), sistema.SPEC)))
    mine = [d for d in drafts if d.kind == "sistema.account-unhealthy"]
    assert len(mine) == 1
    d = mine[0]
    assert d.severity == "critica" and d.deep_link == "/cuenta"  # bloquea la ingesta entera
    assert "gmail-caida" in d.title and "Reconectar" in d.body
    assert d.dedup_key == f"sistema:account-{aid}:reauth_required:{TODAY.isoformat()}"


def test_verificar_cuentas_checks_only_stale_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revalida solo las cuentas con evidencia más vieja que `cada_horas`; no emite avisos (eso lo
    hace `sistema` a partir del estado persistido)."""
    checked: list[int] = []

    async def fake_check(account_id: int, user_id: int) -> None:
        checked.append(account_id)
        return None

    monkeypatch.setattr(verificar_cuentas, "check_account", fake_check)
    with connection() as c:
        fresh = _account(c, "fresca", status="healthy", checked_at=_at(8))
        stale = _account(c, "vieja", status="healthy", checked_at=_at(9) - timedelta(days=2))
        never = _account(c, "nunca", status="unknown", checked_at=None)
        drafts = asyncio.run(verificar_cuentas.produce(_ctx(c, _at(9), verificar_cuentas.SPEC)))
    assert drafts == []
    assert checked == [stale, never] and fresh not in checked


def _subscription(conn: Any, provider: str, *, renews_on: date, status: str = "activa") -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_finance_subscriptions
                  (user_id, provider, provider_key, currency, amount_typical, amount_varies,
                   recurrence, next_renewal_on, status)
                VALUES (1, :p, :k, 'USD', 9.99, :varies,
                        ARRAY['RRULE:FREQ=MONTHLY;BYMONTHDAY=5'], :on, :st)
                RETURNING id
                """
            ),
            {
                "p": provider,
                "k": f"name:{provider}",
                "on": renews_on,
                "st": status,
                "varies": provider == "PorUso",
            },
        ).scalar_one()
    )


def test_renovaciones_avisa_solo_dentro_de_la_ventana() -> None:
    """Avisa de lo que se cobra dentro de `dias_anticipacion`, no de lo lejano."""
    with connection() as c:
        _subscription(c, "Pronto", renews_on=TODAY + timedelta(days=2))
        _subscription(c, "Lejos", renews_on=TODAY + timedelta(days=20))
        drafts = asyncio.run(renovaciones.produce(_ctx(c, _at(9), renovaciones.SPEC)))
    assert [d.facts["provider"] for d in drafts] == ["Pronto"]
    assert "Se renueva Pronto" in drafts[0].title
    assert drafts[0].dedup_key.endswith((TODAY + timedelta(days=2)).isoformat())


def test_renovaciones_ignora_las_que_ya_no_cobran() -> None:
    """`sin_cobros`, cancelada o descartada no generan aviso: ese cobro no va a llegar."""
    with connection() as c:
        for state in ("sin_cobros", "cancelada", "descartada"):
            _subscription(c, f"X-{state}", renews_on=TODAY + timedelta(days=1), status=state)
        assert asyncio.run(renovaciones.produce(_ctx(c, _at(9), renovaciones.SPEC))) == []


def test_renovaciones_dice_que_el_monto_varia() -> None:
    """Facturación por uso: el monto es referencia, no promesa."""
    with connection() as c:
        _subscription(c, "PorUso", renews_on=TODAY)
        draft = asyncio.run(renovaciones.produce(_ctx(c, _at(9), renovaciones.SPEC)))[0]
    assert "hoy" in draft.title and "varía" in draft.body
