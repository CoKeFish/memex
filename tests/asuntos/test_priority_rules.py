"""Reglas 2026-09-10: prioridad (alta|normal|baja; la palabra del usuario manda), «invitación» ya no
abre con un correo, cierre automático por fecha, adherido rutina → null retira la evidencia, y el
asignador parsea/aplica la prioridad."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.assign import SYSTEM_PROMPT, parse_response, run_assign
from memex.modules.asuntos.curator import run_system_rules
from memex.modules.asuntos.settings import AsuntosSettings
from memex.modules.finance.manual import pending_payments
from tests.asuntos._seed import FakeLLM, asunto, evidence, msg, source

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_prompt_declara_las_reglas_nuevas() -> None:
    assert "UN solo asunto por servicio (nombre" not in SYSTEM_PROMPT  # la regla vieja se fue
    assert "invitación, convocatoria o difusión MASIVA" in SYSTEM_PROMPT
    assert (
        "los registra el\nmódulo de finanzas" in SYSTEM_PROMPT
        or "módulo de finanzas" in SYSTEM_PROMPT
    )
    assert '"prioridad":"alta|normal|baja"' in SYSTEM_PROMPT
    assert "responde asunto null y el sistema lo retira" in SYSTEM_PROMPT
    assert "SUSCRIPCIONES de consumo con pago fallido" in SYSTEM_PROMPT
    assert "NO le adhieras recibos ni cobros" in " ".join(SYSTEM_PROMPT.split())


def test_invitacion_ya_no_es_semilla_fuerte(conn: Any) -> None:
    aid = asunto("Semillero X", kind="comunidad", status="candidato", seed="invitacion")
    evidence(aid, msg(source(), subject="Convocatoria semillero"))
    with connection() as c:
        assert not store.promote_if_ready(c, 1, aid, min_evidence_open=2)
        assert store.get_asunto(c, 1, aid).status == "candidato"  # type: ignore[union-attr]


def test_parse_prioridad_en_nuevos_y_actualizaciones() -> None:
    raw = json.dumps(
        {
            "asignaciones": [{"inbox_id": 1, "asunto": "nuevo:1", "confianza": 0.9, "hito": None}],
            "nuevos": [
                {
                    "ref": "nuevo:1",
                    "nombre": "Condonación FONDEX",
                    "tipo": "tramite",
                    "descripcion": "d",
                    "entidades": [],
                    "semilla": "otro",
                    "prioridad": "HIGH",
                    "prioridad_motivo": "plazo y plata en juego",
                }
            ],
            "actualizaciones": [
                {"asunto": "A7", "estado": "x", "cerrado": False, "prioridad": "baja"},
                {"asunto": "A8", "estado": "y", "cerrado": False},
                {"asunto": "A9", "estado": "z", "cerrado": False, "prioridad": "rarísima"},
            ],
            "relaciones": [],
        }
    )
    p = parse_response(raw)
    assert p.new[0].priority == "alta" and p.new[0].priority_reason == "plazo y plata en juego"
    assert [u.priority for u in p.updates] == ["baja", None, None]


def test_set_priority_la_palabra_del_usuario_manda(conn: Any) -> None:
    aid = asunto("Proceso de grado", kind="tramite")
    with connection() as c:
        assert store.set_priority(
            c, 1, aid, "alta", reason="requisito de la condonación", source="llm"
        )
        assert store.set_priority(c, 1, aid, "baja", source="user")
        # ni el asignador ni la regla pisan lo que dijo el usuario
        assert not store.set_priority(c, 1, aid, "alta", source="llm")
        assert not store.set_priority(c, 1, aid, "alta", source="rule")
        a = store.get_asunto(c, 1, aid)
        assert a is not None and (a.priority, a.priority_source) == ("baja", "user")
        with pytest.raises(ValueError):
            store.set_priority(c, 1, aid, "urgente", source="user")


def test_lista_filtra_por_prioridad(conn: Any) -> None:
    a1 = asunto("Alta", kind="tramite")
    a2 = asunto("Baja", kind="comunidad")
    with connection() as c:
        store.set_priority(c, 1, a1, "alta", source="llm")
        store.set_priority(c, 1, a2, "baja", source="llm")
        assert [a.id for a in store.list_asuntos(c, 1, priorities=["alta"])] == [a1]
        assert {a.id for a in store.list_asuntos(c, 1, priorities=["alta", "baja"])} == {a1, a2}


def test_cierre_automatico_por_fecha_solo_tipos_con_fecha(conn: Any) -> None:
    src = source()
    today = date(2026, 9, 10)
    # evento cuyo último hito fue hace un mes y sin señal posterior → se cierra
    ev = asunto("Charla X", kind="evento")
    m = msg(src, subject="Charla X", occurred=datetime(2026, 8, 1, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(
            c, 1, ev, m, method="llm", hito_date=date(2026, 8, 5), hito_text="Charla"
        )
    # evento con hito pasado PERO señal posterior al hito: sigue vivo
    ev2 = asunto("Serie de charlas", kind="evento")
    m2 = msg(src, subject="Charla 1", occurred=datetime(2026, 8, 1, 12, tzinfo=UTC))
    m3 = msg(src, subject="Nueva fecha", occurred=datetime(2026, 9, 1, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, ev2, m2, method="llm", hito_date=date(2026, 8, 5), hito_text="1")
        store.add_evidence(c, 1, ev2, m3, method="llm")
    # trámite con hito pasado: no es tipo con fecha → no se toca
    tr = asunto("Trámite", kind="tramite")
    m4 = msg(src, subject="Radicado", occurred=datetime(2026, 7, 1, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, tr, m4, method="llm", hito_date=date(2026, 7, 2), hito_text="r")
    # evento con hito FUTURO: no se toca
    fut = asunto("Evento futuro", kind="evento")
    m5 = msg(src, subject="Invitación", occurred=datetime(2026, 9, 1, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, fut, m5, method="llm", hito_date=date(2026, 10, 1), hito_text="f")
        assert store.close_expired(c, 1, today=today) == 1
        st = {a.id: a.status for a in store.list_asuntos(c, 1)}
    assert (
        st[ev] == "cerrado"
        and st[ev2] == "abierto"
        and st[tr] == "abierto"
        and st[fut] == "abierto"
    )
    with connection() as c:
        a = store.get_asunto(c, 1, ev)
        assert a is not None and "2026-08-05" in a.state_text
        # idempotente
        assert store.close_expired(c, 1, today=today) == 0


def test_evento_sin_ninguna_fecha_y_semanas_sin_senal_se_cierra(conn: Any) -> None:
    """Caso real (2026-09-17): un hackatón sin hito fechado ni plazo, con última señal en junio,
    seguía «abierto» en septiembre. Un evento ocurre en una fecha: si nadie la dio y hace más de
    `undated_days` que no se habla de él, ya pasó. Con señal reciente, con plazo, con la palabra
    del usuario después de la última señal, o si no es un tipo con fecha, no se toca."""
    src = source()
    today = date(2026, 9, 17)
    junio = datetime(2026, 6, 24, 12, tzinfo=UTC)
    viejo = asunto("Hackatón sin fecha", kind="hackathon")
    reciente = asunto("Hackatón reciente", kind="hackathon")
    con_plazo = asunto("Evento con plazo", kind="evento")
    manual = asunto("Evento reabierto a mano", kind="evento")
    programa = asunto("Programa viejo", kind="programa")
    with connection() as c:
        store.add_evidence(c, 1, viejo, msg(src, subject="h", occurred=junio), method="llm")
        hace_5_dias = datetime(2026, 9, 12, 12, tzinfo=UTC)
        store.add_evidence(
            c, 1, reciente, msg(src, subject="r", occurred=hace_5_dias), method="llm"
        )
        store.add_evidence(c, 1, con_plazo, msg(src, subject="p", occurred=junio), method="llm")
        store.set_due(c, 1, con_plazo, date(2026, 10, 30), source="llm")
        store.add_evidence(c, 1, manual, msg(src, subject="m", occurred=junio), method="llm")
        store.set_status(c, 1, manual, "inactivo", source="system")
        store.set_status(c, 1, manual, "abierto", source="manual")  # la palabra del usuario
        store.add_evidence(c, 1, programa, msg(src, subject="g", occurred=junio), method="llm")
        assert store.close_expired(c, 1, today=today, undated_days=30) == 1
        st = {a.id: a.status for a in store.list_asuntos(c, 1)}
        a = store.get_asunto(c, 1, viejo)
        assert store.close_expired(c, 1, today=today, undated_days=30) == 0  # idempotente
    assert st[viejo] == "cerrado"
    assert st[reciente] == st[con_plazo] == st[manual] == st[programa] == "abierto"
    assert a is not None and "sin fecha conocida" in a.state_text and "2026-06-24" in a.state_text


def test_cli_rules_corre_las_reglas_sin_llm(capsys: Any, conn: Any) -> None:
    from memex.modules.asuntos.cli import main

    ev = asunto("Hackatón viejo", kind="hackathon")
    with connection() as c:
        m = msg(source(), subject="h", occurred=datetime(2026, 5, 1, 12, tzinfo=UTC))
        store.add_evidence(c, 1, ev, m, method="llm")
    assert main(["rules"]) == 0
    assert "closed_by_date': 1" in capsys.readouterr().out
    with connection() as c:
        assert store.get_asunto(c, 1, ev).status == "cerrado"  # type: ignore[union-attr]


def test_system_rules_reportan_cierres_por_fecha(conn: Any) -> None:
    ev = asunto("Hackathon pasado", kind="hackathon")
    m = msg(source(), subject="Hackathon", occurred=datetime(2026, 1, 10, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, ev, m, method="llm", hito_date=date(2026, 1, 12), hito_text="Demo")
        st = run_system_rules(c, 1)
    assert st.closed_by_date == 1


@pytest.mark.asyncio
async def test_adherido_rutina_sin_hito_se_retira_y_con_hito_queda(conn: Any) -> None:
    src = source()
    aid = asunto("Impact Studio")
    m0 = msg(
        src,
        subject="Registration confirmed for Impact Studio Dinner",
        from_addr="a@calendar.luma-mail.com",
        occurred=_T0,
    )
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m0, "asunto": f"A{aid}", "confianza": 0.95, "hito": None}
                ],
                "nuevos": [],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    await run_assign(1, [m0], client=llm, settings=AsuntosSettings())
    # dos adheridos por la llave de evento: uno rutina (null, sin hito) y otro con hito
    m1 = msg(
        src,
        subject="⏰ Impact Studio Dinner is starting soon",
        from_addr="b@calendar.luma-mail.com",
        occurred=_T0 + timedelta(hours=5),
    )
    m2 = msg(
        src,
        subject="Update for Impact Studio Dinner",
        from_addr="b@calendar.luma-mail.com",
        occurred=_T0 + timedelta(hours=6),
    )
    llm2 = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m1, "asunto": None, "confianza": 0.9, "hito": None},
                    {
                        "inbox_id": m2,
                        "asunto": None,
                        "confianza": 0.9,
                        "hito": {"fecha": "2026-03-01", "que": "Cena"},
                    },
                ],
                "nuevos": [],
                "actualizaciones": [
                    {"asunto": f"A{aid}", "estado": "cena", "cerrado": False, "prioridad": "alta"}
                ],
                "relaciones": [],
            }
        )
    )
    res = await run_assign(1, [m1, m2], client=llm2, settings=AsuntosSettings())
    assert res.adhered == 2
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT inbox_id, status FROM mod_asuntos_evidence WHERE asunto_id = :a "
                "ORDER BY inbox_id"
            ),
            {"a": aid},
        ).all()
        seen: dict[int, int | None] = {
            int(r[0]): (int(r[1]) if r[1] is not None else None)
            for r in c.execute(
                text("SELECT inbox_id, asunto_id FROM mod_asuntos_seen WHERE inbox_id IN (:x, :y)"),
                {"x": m1, "y": m2},
            ).all()
        }
        a = store.get_asunto(c, 1, aid)
    status = {int(r[0]): str(r[1]) for r in rows}
    assert status[m1] == "rejected" and status[m2] == "active"  # rutina fuera; con hito se queda
    assert seen[m1] is None and seen[m2] == aid
    assert a is not None and a.priority == "alta" and a.priority_source == "llm"


@pytest.mark.asyncio
async def test_nuevo_nace_con_prioridad(conn: Any) -> None:
    src = source()
    m = msg(src, subject="Condonación", from_addr="fondex@fondex.gov.co", occurred=_T0)
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m, "asunto": "nuevo:1", "confianza": 0.9, "hito": None}
                ],
                "nuevos": [
                    {
                        "ref": "nuevo:1",
                        "nombre": "Condonación crédito FONDEX",
                        "tipo": "tramite",
                        "descripcion": "d",
                        "entidades": ["FONDEX"],
                        "semilla": "otro",
                        "prioridad": "alta",
                        "prioridad_motivo": "plata en juego; depende del grado",
                    }
                ],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    await run_assign(1, [m], client=llm, settings=AsuntosSettings())
    with connection() as c:
        a = store.list_asuntos(c, 1, q="FONDEX")[0]
    assert (a.priority, a.priority_source) == ("alta", "llm") and "grado" in a.priority_reason


def test_pagos_pendientes_sin_regularizar(conn: Any) -> None:
    def tx(status: str, counterparty: str, when: datetime, amount: float = 10.0) -> None:
        with connection() as c:
            c.execute(
                text(
                    "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, "
                    "amount, currency, category, counterparty, place, occurred_at, "
                    "occurred_at_precision, "
                    "description) VALUES (1, 'egreso', :s, 'pago', :a, 'USD', 'software', :cp, '', "
                    ":o, 'datetime', '')"
                ),
                {"s": status, "a": amount, "cp": counterparty, "o": when},
            )

    t = datetime(2026, 9, 1, 12, tzinfo=UTC)
    tx("fallida", "Notion", t)  # sin pago posterior → pendiente
    tx("fallida", "Nubix", t)  # regularizado 3 días después → no aparece
    tx("completada", "Nubix", t + timedelta(days=3))
    tx("pendiente", "Tigo", t - timedelta(days=40))  # viejo, sigue pendiente
    tx("fallida", "Uber", t - timedelta(days=60))
    tx("completada", "uber", t - timedelta(days=59))  # case-insensitive
    with connection() as c:
        rows = pending_payments(c, 1, resolve_days=30)
    assert [r["counterparty"] for r in rows] == ["Notion", "Tigo"]
    assert all(r["age_days"] >= 0 for r in rows)


@pytest.mark.asyncio
async def test_force_conserva_el_cierre_y_cierra_por_fecha_al_final(conn: Any) -> None:
    """Re-decidir con --force no reabre lo cerrado: el asunto cerrado al que el modelo vuelve a
    adherir sigue cerrado (solo `cerrado=false` lo reabre), y un evento con el último hito ya pasado
    se cierra al terminar la corrida sin esperar al mantenimiento."""
    src = source()
    cerrado = asunto("Hackathon pasado", kind="hackathon", status="cerrado")
    evento = asunto("Charla vieja", kind="evento", status="abierto")
    m1 = msg(src, subject="Gracias por participar", occurred=datetime(2026, 1, 12, 12, tzinfo=UTC))
    m2 = msg(src, subject="Charla el 5", occurred=datetime(2026, 1, 3, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, cerrado, m1, method="llm")
        store.add_evidence(c, 1, evento, m2, method="llm")
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m1, "asunto": f"A{cerrado}", "confianza": 0.9, "hito": None},
                    {
                        "inbox_id": m2,
                        "asunto": f"A{evento}",
                        "confianza": 0.9,
                        "hito": {"fecha": "2026-01-05", "que": "Charla"},
                    },
                ],
                "nuevos": [],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    res = await run_assign(1, [m1, m2], client=llm, settings=AsuntosSettings(), force=True)
    assert res.assigned == 2 and res.purged == 0
    assert res.closed_by_date == 1
    with connection() as c:
        st = {a.id: a.status for a in store.list_asuntos(c, 1)}
    assert st[cerrado] == "cerrado" and st[evento] == "cerrado"


def test_parse_vence_en_nuevos_y_tri_estado_en_actualizaciones() -> None:
    raw = json.dumps(
        {
            "asignaciones": [{"inbox_id": 1, "asunto": "nuevo:1", "confianza": 0.9, "hito": None}],
            "nuevos": [
                {
                    "ref": "nuevo:1",
                    "nombre": "Proyecto X",
                    "tipo": "proyecto",
                    "descripcion": "d",
                    "entidades": [],
                    "semilla": "otro",
                    "prioridad": "alta",
                    "vence": "2026-08-29",
                }
            ],
            "actualizaciones": [
                {"asunto": "A7", "estado": "x", "cerrado": False, "vence": "2026-09-14"},
                {"asunto": "A8", "estado": "y", "cerrado": False, "vence": None},
                {"asunto": "A9", "estado": "z", "cerrado": False},
            ],
            "relaciones": [],
        }
    )
    p = parse_response(raw)
    assert p.new[0].due_on == date(2026, 8, 29)
    assert [(u.due_set, u.due_on) for u in p.updates] == [
        (True, date(2026, 9, 14)),
        (True, None),
        (False, None),
    ]


def test_plazo_vencido_sin_senal_cierra_cualquier_tipo(conn: Any) -> None:
    src = source()
    today = date(2026, 9, 10)
    vencido = asunto("agent-bazaar-mcp", kind="proyecto", status="abierto")
    vivo = asunto("Gemini API", kind="tramite", status="abierto")
    con_senal = asunto("Trámite con novedad", kind="tramite", status="abierto")
    del_usuario = asunto("Mi plazo", kind="tramite", status="abierto")
    m1 = msg(src, subject="Deletion", occurred=datetime(2026, 8, 27, 12, tzinfo=UTC))
    m2 = msg(src, subject="Billing", occurred=datetime(2026, 8, 18, 12, tzinfo=UTC))
    m3 = msg(src, subject="Plazo", occurred=datetime(2026, 8, 1, 12, tzinfo=UTC))
    m4 = msg(src, subject="Respuesta", occurred=datetime(2026, 9, 5, 12, tzinfo=UTC))
    m5 = msg(src, subject="x", occurred=datetime(2026, 8, 1, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, vencido, m1, method="llm")
        store.add_evidence(c, 1, vivo, m2, method="llm")
        store.add_evidence(c, 1, con_senal, m3, method="llm")
        store.add_evidence(c, 1, con_senal, m4, method="llm")
        store.add_evidence(c, 1, del_usuario, m5, method="llm")
        store.set_due(c, 1, vencido, date(2026, 8, 29))  # pasó hace 12 días, sin señal después
        store.set_due(c, 1, vivo, date(2026, 9, 14))  # futuro
        store.set_due(c, 1, con_senal, date(2026, 8, 15))  # pasó, pero hubo correo el 5 sep
        store.set_due(c, 1, del_usuario, date(2026, 8, 10))  # pasó, pero el usuario lo reabrió
        store.set_status(c, 1, del_usuario, "abierto", source="manual")
        assert store.close_expired(c, 1, today=today) == 1
        st = {a.id: (a.status, a.state_text) for a in store.list_asuntos(c, 1)}
    assert st[vencido][0] == "cerrado" and "2026-08-29" in st[vencido][1]
    assert st[vivo][0] == "abierto" and st[con_senal][0] == "abierto"
    assert st[del_usuario][0] == "abierto"


def test_set_due_respeta_la_palabra_del_usuario_y_sube_prioridad_por_plazo(conn: Any) -> None:
    today = datetime.now(UTC).date()
    aid = asunto("Gemini API", kind="tramite", status="abierto")
    m = msg(source(), subject="x", occurred=datetime.now(UTC))
    with connection() as c:
        store.add_evidence(c, 1, aid, m, method="llm")
        assert store.set_due(c, 1, aid, today + timedelta(days=4), source="llm")
        assert store.raise_priority_for_deadlines(c, 1, within_days=14) == 1
        a = store.get_asunto(c, 1, aid)
        assert a is not None and a.priority == "alta" and a.priority_source == "rule"
        assert store.set_due(c, 1, aid, today + timedelta(days=40), source="user")
        assert not store.set_due(c, 1, aid, None, source="llm")  # el modelo no pisa al usuario
        assert store.get_asunto(c, 1, aid).due_on == today + timedelta(days=40)  # type: ignore[union-attr]


def test_plazo_vencido_con_senal_posterior_se_borra_salvo_el_del_usuario(conn: Any) -> None:
    src = source()
    today = date(2026, 9, 10)
    viejo = asunto("Google Cloud", kind="servicio", status="abierto")
    del_usuario = asunto("Lo mío", kind="tramite", status="abierto")
    m1 = msg(src, subject="Cargos", occurred=datetime(2026, 6, 20, 12, tzinfo=UTC))
    m2 = msg(src, subject="Aviso", occurred=datetime(2026, 9, 2, 12, tzinfo=UTC))
    m3 = msg(src, subject="x", occurred=datetime(2026, 9, 2, 12, tzinfo=UTC))
    with connection() as c:
        store.add_evidence(c, 1, viejo, m1, method="llm")
        store.add_evidence(c, 1, viejo, m2, method="llm")
        store.add_evidence(c, 1, del_usuario, m3, method="llm")
        store.set_due(c, 1, viejo, date(2026, 7, 1))  # pasó, y hubo correo el 2 sep
        store.set_due(c, 1, del_usuario, date(2026, 7, 1), source="user")
        assert store.clear_stale_due(c, 1, today=today) == 1
        assert store.close_expired(c, 1, today=today) == 0  # nada que cerrar: hubo señal después
        a, b = store.get_asunto(c, 1, viejo), store.get_asunto(c, 1, del_usuario)
    assert a is not None and a.due_on is None and a.status == "abierto"
    assert b is not None and b.due_on == date(2026, 7, 1)  # la palabra del usuario se conserva


def _pago(
    counterparty: str, when: datetime, amount: float = 10.0, status: str = "completada"
) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, amount, "
                "currency, category, counterparty, place, occurred_at, occurred_at_precision, "
                "description) VALUES (1, 'egreso', :s, 'pago', :a, 'COP', 'software', :cp, '', "
                ":o, 'datetime', '')"
            ),
            {"s": status, "a": amount, "cp": counterparty, "o": when},
        )


def test_asunto_de_pago_se_cierra_con_el_pago_posterior_en_finanzas(conn: Any) -> None:
    from memex.modules.asuntos.finance_link import close_paid, is_payment_asunto

    src = source()
    aviso = datetime(2026, 9, 2, 12, tzinfo=UTC)
    gcp = asunto("Pagos Google Cloud (proyecto n8n-correo)", kind="servicio", status="abierto")
    grado = asunto("Proceso de grado Ingeniería", kind="tramite", status="abierto")
    viejo = asunto("Pagos Tigo", kind="servicio", status="abierto")
    mio = asunto("Pagos Notion", kind="servicio", status="abierto")
    for aid, subj in (
        (gcp, "Acción necesaria: cargos"),
        (grado, "Requisitos"),
        (viejo, "Tigo"),
        (mio, "Notion"),
    ):
        m = msg(src, subject=subj, occurred=aviso)
        with connection() as c:
            store.add_evidence(c, 1, aid, m, method="llm")
    with connection() as c:
        store.update_fields(c, 1, gcp, entities=["Google Cloud"])
        store.record_state(c, 1, gcp, "cargos pendientes en la cuenta", closed=False, source="llm")
        store.record_state(c, 1, grado, "falta requisito de lengua", closed=False, source="llm")
        store.record_state(c, 1, viejo, "deuda pendiente", closed=False, source="llm")
        store.record_state(c, 1, mio, "pago fallido", closed=False, source="llm")
        store.set_status(c, 1, mio, "abierto", source="manual")  # palabra del usuario después
        assert is_payment_asunto(store.get_asunto(c, 1, gcp))  # type: ignore[arg-type]
        assert not is_payment_asunto(store.get_asunto(c, 1, grado))  # type: ignore[arg-type]
    _pago("Google Cloud Platform & APIs", aviso + timedelta(days=3), 154804)
    _pago("Pontificia Universidad Salesiana", aviso + timedelta(days=3), 500000)
    _pago("Tigo", aviso - timedelta(days=30))  # ANTES del aviso: no regulariza
    _pago("Notion Labs", aviso + timedelta(days=1))
    with connection() as c:
        assert close_paid(c, 1) == 1
        st = {a.id: (a.status, a.state_text) for a in store.list_asuntos(c, 1)}
    assert st[gcp][0] == "cerrado" and "154804" in st[gcp][1] and "Google Cloud" in st[gcp][1]
    assert st[grado][0] == "abierto"  # no es asunto de pago
    assert st[viejo][0] == "abierto"  # el pago fue anterior al aviso
    assert st[mio][0] == "abierto"  # la palabra del usuario manda
