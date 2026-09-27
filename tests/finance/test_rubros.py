"""Rubro por contraparte y reclasificación de «otros».

La regla manda al registrar, al consolidar y hacia atrás; el dueño pisa al modelo y no al revés;
la reclasificación resuelve lo obvio sin modelo, le pregunta el resto en lotes (FakeLLM) y solo
aplica lo que vuelve con confianza. «Otros» sigue siendo «otros» cuando no se sabe.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm.client import LLMResult, LLMUsage
from memex.modules.finance import rubros
from memex.modules.finance.cli import main as finance_main
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.module import register
from memex.modules.finance.schema import FINANCE_CATEGORIES

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class FakeLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, messages: Any, **_: Any) -> LLMResult:
        self.prompts.append(messages[-1].content)
        content = self.responses.pop(0) if self.responses else '{"rubros": []}'
        return LLMResult(
            content=content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0.0001"),
            latency_ms=1,
            finish_reason="stop",
        )


def _cat_of(conn: Connection, cons_id: int) -> str:
    return str(
        conn.execute(
            text("SELECT category FROM mod_finance_consolidated WHERE id = :i"), {"i": cons_id}
        ).scalar_one()
    )


def _cat_committed(cons_id: int) -> str:
    """Lee por una conexión propia: lo que otros escribieron con la suya (commiteado)."""
    with connection() as c:
        return _cat_of(c, cons_id)


def _reg(conn: Connection, counterparty: str, **kw: Any) -> int:
    row = register(
        conn,
        1,
        amount=kw.pop("amount", Decimal("10000")),
        currency="COP",
        counterparty=counterparty,
        **kw,
    )
    return int(row["consolidated_id"])


# --------------------------------------------------------------------- la lista


def test_la_lista_tiene_los_rubros_nuevos_y_otros_al_final() -> None:
    for c in ("compras", "transferencia", "traspaso", "ingreso", "rendimientos"):
        assert c in FINANCE_CATEGORIES
    assert FINANCE_CATEGORIES[-1] == "otros"
    assert "SOLO cuando no se sabe" in rubros.CATEGORY_GUIDE


# --------------------------------------------------------------------- reglas


def test_set_list_remove_y_el_dueno_pisa_al_modelo(conn: Connection) -> None:
    r = rubros.set_rule(conn, 1, "Nacanisaje SAS", "comida", note="restaurante")
    assert r is not None and r["source"] == "user"
    # el modelo no pisa al dueño
    assert rubros.set_rule(conn, 1, "NACANISAJE  sas", "compras", source="llm") is None
    assert rubros.rule_for(conn, 1, "nacanisaje sas") == "comida"
    # el dueño sí pisa al modelo
    assert rubros.set_rule(conn, 1, "Viki Com", "otros", source="llm") is not None
    r2 = rubros.set_rule(conn, 1, "Viki Com", "entretenimiento")
    assert r2 is not None and r2["source"] == "user" and r2["category"] == "entretenimiento"
    assert [x["counterparty"] for x in rubros.list_rules(conn, 1)] == ["Nacanisaje SAS", "Viki Com"]
    assert rubros.remove_rule(conn, 1, "viki com") and not rubros.remove_rule(conn, 1, "viki com")
    with pytest.raises(ValueError):
        rubros.set_rule(conn, 1, "X", "no-existe")
    with pytest.raises(ValueError):
        rubros.set_rule(conn, 1, "  ", "comida")


def test_la_regla_manda_al_registrar(conn: Connection) -> None:
    rubros.set_rule(conn, 1, "Nacanisaje SAS", "comida")
    cid = _reg(conn, "NACANISAJE SAS", category="otros")
    assert _cat_of(conn, cid) == "comida"
    # sin regla, lo que vino
    cid2 = _reg(conn, "SEN P", category="otros")
    assert _cat_of(conn, cid2) == "otros"


def test_la_regla_se_aplica_hacia_atras_y_al_consolidar() -> None:
    with connection() as c:
        a = _reg(c, "Seratta", category="otros")
        b = _reg(c, "SERATTA", category="otros", amount=Decimal("55000"))
        u = _reg(c, "Uber", category="transporte")
        assert _cat_of(c, a) == "otros"
        rubros.set_rule(c, 1, "Seratta", "comida")
        assert rubros.apply_rule(c, 1, "seratta", "comida") == 2
        assert _cat_of(c, a) == "comida" and _cat_of(c, b) == "comida"
        assert _cat_of(c, u) == "transporte"
        crudas = c.execute(
            text(
                "SELECT count(*) FROM mod_finance_transactions "
                "WHERE user_id = 1 AND memex_norm(counterparty) = 'seratta' AND category = 'comida'"
            )
        ).scalar_one()
        assert crudas == 2


def test_el_reconciliador_batch_respeta_la_regla() -> None:
    """Una cruda que llega sin consolidado (backfill) y dice «otros»: al consolidar manda la
    regla. El batch es estable (no reescribe filas cuya membresía no cambió), así que se prueba
    con crudas nuevas."""
    with connection() as c:
        rubros.set_rule(c, 1, "Seratta", "comida")
        tid = c.execute(
            text(
                "INSERT INTO mod_finance_transactions (user_id, source_inbox_ids, direction, "
                "status, kind, amount, currency, category, counterparty, place, occurred_at, "
                "occurred_at_precision, description, evidence) VALUES (1, '{}', 'egreso', "
                "'completada', 'pago', 43000, 'COP', 'otros', 'SERATTA', '', NOW(), 'datetime', "
                "'Compra en Seratta', 'x') RETURNING id"
            )
        ).scalar_one()
    run_consolidation(1)
    with connection() as c:
        cat = c.execute(
            text(
                "SELECT cons.category FROM mod_finance_consolidated cons "
                "JOIN mod_finance_transaction_links l ON l.consolidated_id = cons.id "
                "WHERE l.transaction_id = :t"
            ),
            {"t": tid},
        ).scalar_one()
    assert cat == "comida"


# --------------------------------------------------------------------- reclasificación

#: Lo que contesta el modelo para el lote, que va por cantidad de movimientos y luego llave:
#: [SEN P, Unity] (Bre-B ya se resolvió determinista). Unity seguro; SEN P con confianza
#: baja (no se aplica).
RESPUESTA = json.dumps(
    {
        "rubros": [
            {"k": 1, "rubro": "compras", "confianza": 0.3},
            {"k": 2, "rubro": "software", "confianza": 0.95},
        ]
    }
)


def _seed_otros(conn: Connection) -> dict[str, int]:
    return {
        "nac": _reg(conn, "Nacanisaje SAS", category="otros"),
        "recarga": _reg(
            conn,
            "Bancolombia",
            category="otros",
            direction="ingreso",
            description="Recarga desde Bancolombia",
        ),
        "unity": _reg(conn, "Unity", category="otros"),
        "senp": _reg(conn, "SEN P", category="otros"),
        "breb": _reg(
            conn,
            "",
            category="otros",
            direction="ingreso",
            description="Envío de 30.000 COP por Bre-B",
        ),
        "breb2": _reg(
            conn,
            "",
            category="otros",
            direction="ingreso",
            amount=Decimal("80000"),
            description="Envío de 80.000 COP por Bre-B",
        ),
        "uber": _reg(conn, "Uber", category="transporte"),
    }


def test_recategorize_regla_determinista_modelo_y_riendas() -> None:
    with connection() as conn:
        ids = _seed_otros(conn)
        rubros.set_rule(conn, 1, "Nacanisaje SAS", "comida")  # ya lo dijo el dueño

    with connection() as c:
        grupos = rubros.load_groups(c, 1)
    claves = {g.key: g for g in grupos}
    assert not any(k.startswith("cp:uber|") for k in claves)  # no estaba en otros
    breb = claves["desc:envio de cop por bre b"]
    assert breb.n == 2 and breb.counterparty == "" and breb.ingresos == 2

    # dry-run: decide, no escribe, y muestra qué haría
    llm = FakeLLM(RESPUESTA)
    st = asyncio.run(rubros.recategorize(1, dry_run=True, llm=llm))
    assert st.dry_run and st.consolidados_cambiados == 0
    assert _cat_committed(ids["unity"]) == "otros"
    assert st.por_regla == 1 and st.deterministas == 2 and st.por_modelo == 1 and st.sin_cambio == 1
    # el lote que vio el modelo: solo lo no resuelto antes, numerado, con sentido y descripción
    lote = llm.prompts[0]
    assert "1. contraparte: Unity" in lote or "contraparte: Unity" in lote
    assert "Nacanisaje" not in lote and "Recarga desde" not in lote
    assert "(sin contraparte)" not in lote  # Bre-B se resolvió sin modelo
    por = {d["contraparte"] or d["descripcion"]: (d["rubro"], d["por"]) for d in st.decisiones}
    assert por["Nacanisaje SAS"] == ("comida", "regla")
    assert por["Bancolombia"] == ("traspaso", "determinista")
    assert por["SEN P"] == ("otros", "")

    # de verdad: escribe, deja reglas llm y aplica por ids lo que no tiene contraparte
    llm2 = FakeLLM(RESPUESTA)
    st2 = asyncio.run(rubros.recategorize(1, llm=llm2))
    assert st2.consolidados_cambiados == 5  # nac + recarga + unity + breb x2
    assert _cat_committed(ids["nac"]) == "comida"
    assert _cat_committed(ids["recarga"]) == "traspaso"
    assert _cat_committed(ids["unity"]) == "software"
    assert _cat_committed(ids["senp"]) == "otros"
    assert (
        _cat_committed(ids["breb"]) == "transferencia"
        and _cat_committed(ids["breb2"]) == "transferencia"
    )
    assert _cat_committed(ids["uber"]) == "transporte"
    with connection() as c:
        reglas = {r["counterparty"]: (r["category"], r["source"]) for r in rubros.list_rules(c, 1)}
    assert reglas["Unity"] == ("software", "llm")
    assert "Bancolombia" not in reglas  # determinista por descripción: no es regla de contraparte
    assert reglas["Nacanisaje SAS"] == ("comida", "user")
    assert "SEN P" not in reglas
    # y el próximo cargo del mismo comercio ya sale bien
    with connection() as c:
        nuevo = _reg(c, "UNITY", category="otros")
        assert _cat_of(c, nuevo) == "software"


def test_lo_que_la_descripcion_ya_dice_no_pasa_por_el_modelo() -> None:
    def g(desc: str) -> rubros.Grupo:
        return rubros.Grupo(key="x", counterparty="", descriptions=[desc])

    assert rubros.decide_deterministic(g("Recarga desde Bancolombia")) == "traspaso"
    assert rubros.decide_deterministic(g("Retiro en cajero")) == "traspaso"
    assert rubros.decide_deterministic(g("Pago de intereses")) == "rendimientos"
    assert rubros.decide_deterministic(g("Recibí a mi llave de Giseth")) == "transferencia"
    assert rubros.decide_deterministic(g("Recibido por Bre-B de André")) == "transferencia"
    assert rubros.decide_deterministic(g("Envío de 30.000 COP por Bre-B a X")) == "transferencia"
    assert rubros.decide_deterministic(g("Envío Transfiya a 3222521325")) == "transferencia"
    assert rubros.decide_deterministic(g("Compra en Nacanisaje SAS")) is None
    assert rubros.MIN_CONFIDENCE == 0.7


def test_parse_batch_tolerante() -> None:
    assert rubros.parse_batch("no json", 3) == {}
    out = rubros.parse_batch(
        json.dumps(
            {
                "rubros": [
                    {"k": 1, "rubro": "Comida", "confianza": 0.8},
                    {"k": 9, "rubro": "comida", "confianza": 0.8},  # fuera de rango
                    {"k": 2, "rubro": "invento", "confianza": 0.8},  # rubro inválido
                    {"k": "3", "rubro": "otros", "confianza": "1.5"},  # se acota
                    "basura",
                ]
            }
        ),
        3,
    )
    assert out == {1: ("comida", 0.8), 3: ("otros", 1.0)}


# --------------------------------------------------------------------- API


def test_api_rubros(client: Any) -> None:
    with connection() as c:
        a = _reg(c, "Poder BGTA", category="otros")
    r = client.post("/finance/rubros", json={"counterparty": "Poder BGTA", "category": "salud"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1 and body["rule"]["category"] == "salud"
    assert _cat_committed(a) == "salud"
    assert client.get("/finance/rubros").json()["items"][0]["counterparty"] == "Poder BGTA"
    assert (
        client.post("/finance/rubros", json={"counterparty": "X", "category": "nada"}).status_code
        == 422
    )
    assert client.post("/finance/rubros/remove", json={"counterparty": "poder bgta"}).json() == {
        "removed": True
    }
    assert client.get("/finance/rubros").json()["items"] == []


# --------------------------------------------------------------------- CLI


def test_cli_rubro_y_recategorize(capsys: Any, monkeypatch: Any) -> None:
    with connection() as c:
        a = _reg(c, "Poder BGTA", category="otros")
        _reg(c, "Unity", category="otros")
    assert finance_main(["rubro", "--counterparty", "Poder BGTA", "--category", "salud"]) == 0
    out = capsys.readouterr().out
    assert "«Poder BGTA» → salud · 1 pagos actualizados" in out
    assert _cat_committed(a) == "salud"
    assert finance_main(["rubro", "--list"]) == 0
    assert "Poder BGTA → salud [user]" in capsys.readouterr().out
    assert finance_main(["rubro", "--counterparty", "X"]) == 2  # sin --category

    monkeypatch.setattr(
        rubros,
        "build_llm_client",
        lambda *a, **k: FakeLLM(
            json.dumps({"rubros": [{"k": 1, "rubro": "software", "confianza": 0.9}]})
        ),
    )
    assert finance_main(["recategorize", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Unity" in out and "→ software [modelo 0.9]" in out and "(dry-run)" in out
    assert finance_main(["rubro", "--remove", "poder bgta"]) == 0
    assert "regla quitada" in capsys.readouterr().out


def test_una_contraparte_heterogenea_no_gana_regla() -> None:
    """Una billetera tiene intereses (rendimientos) y gravámenes (servicios): cada grupo va a lo
    suyo y NO queda una regla por contraparte que pise lo demás (el daño de la primera pasada)."""
    with connection() as c:
        intereses = _reg(
            c, "Nequi", category="otros", direction="ingreso", description="Pago de intereses"
        )
        gravamen = _reg(c, "Nequi", category="otros", description="Gravamen al movimiento")
        comision = _reg(c, "Nequi", category="servicios", description="IVA sobre comisión")
    llm = FakeLLM(json.dumps({"rubros": [{"k": 1, "rubro": "servicios", "confianza": 0.9}]}))
    st = asyncio.run(rubros.recategorize(1, llm=llm))
    assert st.deterministas == 1 and st.por_modelo == 1
    assert _cat_committed(intereses) == "rendimientos"
    assert _cat_committed(gravamen) == "servicios"
    assert _cat_committed(comision) == "servicios"
    with connection() as c:
        assert rubros.list_rules(c, 1) == []


def test_la_regla_llm_solo_si_todo_lo_de_la_contraparte_coincide() -> None:
    with connection() as c:
        a = _reg(c, "DigitalOcean", category="otros", description="Compra en DigitalOcean")
        b = _reg(
            c,
            "DigitalOcean",
            category="otros",
            direction="ingreso",
            description="Promotional credit received",
        )
    llm = FakeLLM(
        json.dumps(
            {
                "rubros": [
                    {"k": 1, "rubro": "software", "confianza": 0.9},
                    {"k": 2, "rubro": "rendimientos", "confianza": 0.8},
                ]
            }
        )
    )
    asyncio.run(rubros.recategorize(1, llm=llm))
    assert _cat_committed(a) == "software" and _cat_committed(b) == "rendimientos"
    with connection() as c:
        assert rubros.list_rules(c, 1) == []  # dos rubros distintos: sin regla
    # si en cambio hay acuerdo, la regla queda y el próximo cargo sale bien
    with connection() as c:
        u1 = _reg(c, "Unity", category="otros", description="Compra en Unity")
        u2 = _reg(c, "Unity", category="otros", amount=Decimal("99000"), description="Unity Pro")
    llm2 = FakeLLM(
        json.dumps(
            {
                "rubros": [
                    {"k": 1, "rubro": "software", "confianza": 0.9},
                    {"k": 2, "rubro": "software", "confianza": 0.9},
                ]
            }
        )
    )
    asyncio.run(rubros.recategorize(1, llm=llm2))
    assert _cat_committed(u1) == "software" and _cat_committed(u2) == "software"
    with connection() as c:
        assert [(r["counterparty"], r["source"]) for r in rubros.list_rules(c, 1)] == [
            ("Unity", "llm")
        ]
