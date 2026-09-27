"""«Contar algo»: la nota del dueño queda como hito y, si trae fecha o algo hecho, se aplica.

Sin red: `anotar(llm=FakeLLM(...))` reemplaza al intérprete. Lo que se prueba es el contrato
—qué se le da al modelo, qué se hace con lo que devuelve y con qué riendas— y que la nota se
guarde pase lo que pase con el modelo.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import notas, store
from memex.modules.tareas import module as tareas
from tests.asuntos._seed import FakeLLM, asunto

HOY = date(2026, 9, 16)  # miércoles


def _con_plan(aid: int) -> dict[str, int]:
    """Un proyecto con dos pasos colgando del asunto, como los deja el planificador."""
    with connection() as c:
        pid = tareas.find_or_create_parent(c, 1, title="Sacar el pasaporte", asunto_id=aid)
        ir = tareas.add(
            c,
            1,
            title="Solicitar el pasaporte presencialmente",
            asunto=aid,
            parent=pid,
            origin="manual",
        )
        pagar = tareas.add(c, 1, title="Pagar el trámite", asunto=aid, parent=pid, origin="manual")
    return {"proyecto": pid, "ir": int(ir["id"]), "pagar": int(pagar["id"])}


def _tarea(tid: int) -> dict[str, Any]:
    with connection() as c:
        return tareas.get(c, 1, tid)


def _notas(aid: int) -> list[dict[str, Any]]:
    with connection() as c:
        return notas.list_notas(c, 1, aid)


def _respuesta(**kw: Any) -> str:
    base: dict[str, Any] = {"entendido": "", "pasos": [], "vence": None, "estado": None}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# --------------------------------------------------------------------- el prompt


def test_el_calendario_vuelve_inequivoco_el_viernes() -> None:
    cal = notas.calendario(HOY)
    assert cal.startswith("Hoy es miércoles 2026-09-16")
    assert "viernes 18 = 2026-09-18" in cal
    assert "martes 22 = 2026-09-22" in cal


def test_el_prompt_lleva_los_pasos_con_su_id_y_la_nota(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    ids = _con_plan(aid)
    with connection() as c:
        a = store.get_asunto(c, 1, aid)
        pend = notas.pendientes_de(c, 1, aid)
    assert a is not None
    msgs = notas.build_messages(asunto=a, pendientes=pend, texto="el viernes voy", hoy=HOY)
    user = msgs[-1].content
    assert f"id {ids['ir']}: Solicitar el pasaporte presencialmente" in user
    assert f"id {ids['proyecto']}: Sacar el pasaporte (proyecto)" in user
    assert "## NOTA DE LA PERSONA\nel viernes voy" in user
    assert "NUNCA inventes" in msgs[0].content


# --------------------------------------------------------------------- el parseo


def test_parse_tolera_basura_y_toma_lo_valido() -> None:
    assert notas.parse_interpretacion("no soy json").pasos == ()
    assert notas.parse_interpretacion("[]").estado is None
    i = notas.parse_interpretacion(
        json.dumps(
            {
                "entendido": "  va el   viernes ",
                "pasos": [
                    {"id": "12", "fecha": "2026-09-18", "hecho": False},
                    {"id": "x", "fecha": "2026-09-18"},
                    "no soy objeto",
                    {"id": 13, "fecha": "no es fecha", "hecho": True},
                ],
                "vence": "2026-09-30",
                "estado": "   ",
            }
        )
    )
    assert i.entendido == "va el viernes"
    assert [(p.id, p.fecha, p.hecho) for p in i.pasos] == [
        (12, date(2026, 9, 18), False),
        (13, None, True),
    ]
    assert i.vence == date(2026, 9, 30) and i.estado is None


# --------------------------------------------------------------------- aplicar


def test_la_nota_le_pone_fecha_al_paso_y_queda_como_hito(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    ids = _con_plan(aid)
    llm = FakeLLM(
        _respuesta(
            entendido="Va a la sede el viernes en la tarde.",
            pasos=[{"id": ids["ir"], "fecha": "2026-09-18", "hecho": False}],
        )
    )
    res = asyncio.run(
        notas.anotar(
            1,
            aid,
            "me dijeron que el viernes en la tarde puedo pasar o cualquier otro día",
            llm=llm,
            today=HOY,
        )
    )
    assert res.interpretada and res.entendido == "Va a la sede el viernes en la tarde."
    assert res.cambios == ["«Solicitar el pasaporte presencialmente»: viernes 18 sep"]
    assert _tarea(ids["ir"])["due_on"] == "2026-09-18"
    assert _tarea(ids["pagar"])["due_on"] is None
    guardadas = _notas(aid)
    assert len(guardadas) == 1
    assert guardadas[0]["texto"].startswith("me dijeron que el viernes")
    assert guardadas[0]["cambios"] == res.cambios
    assert guardadas[0]["entendido"] == res.entendido
    # el prompt que vio el modelo llevaba la nota y el calendario
    assert "viernes 18 = 2026-09-18" in llm.prompts[0]


def test_paso_hecho_y_estado_nuevo(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    ids = _con_plan(aid)
    llm = FakeLLM(
        _respuesta(
            entendido="Ya pagó.",
            pasos=[{"id": ids["pagar"], "fecha": None, "hecho": True}],
            estado="Pagado; falta ir a la sede a solicitarlo.",
        )
    )
    res = asyncio.run(notas.anotar(1, aid, "ya pagué los 195.600", llm=llm, today=HOY))
    assert res.cambios == [
        "Hecho: «Pagar el trámite»",
        "Estado: Pagado; falta ir a la sede a solicitarlo.",
    ]
    assert _tarea(ids["pagar"])["status"] == "completada"
    with connection() as c:
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.state_text == "Pagado; falta ir a la sede a solicitarlo."


def test_riendas_cortas_ids_ajenos_fechas_lejanas_y_plazo(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    otro = asunto("Otro trámite", kind="tramite")
    ids = _con_plan(aid)
    ajeno = _con_plan(otro)
    llm = FakeLLM(
        _respuesta(
            entendido="x",
            pasos=[
                {"id": ajeno["ir"], "fecha": "2026-09-18", "hecho": False},  # de otro asunto
                {"id": 999999, "fecha": "2026-09-18", "hecho": False},  # no existe
                {"id": ids["ir"], "fecha": "2030-01-01", "hecho": False},  # fuera de rango
            ],
            vence="2026-09-30",
        )
    )
    res = asyncio.run(notas.anotar(1, aid, "tengo hasta el 30", llm=llm, today=HOY))
    assert res.cambios == ["Plazo del asunto: miércoles 30 sep"]
    assert _tarea(ajeno["ir"])["due_on"] is None
    assert _tarea(ids["ir"])["due_on"] is None
    with connection() as c:
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.due_on == date(2026, 9, 30)


def test_sin_interprete_la_nota_se_guarda_igual(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")

    class Roto:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("proveedor caído")

    res = asyncio.run(notas.anotar(1, aid, "el viernes voy", llm=Roto(), today=HOY))
    assert not res.interpretada and res.cambios == []
    assert [n["texto"] for n in _notas(aid)] == ["el viernes voy"]


def test_solo_anotar_no_llama_al_modelo(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    llm = FakeLLM()
    res = asyncio.run(notas.anotar(1, aid, "dato suelto", llm=llm, interpretar=False))
    assert not res.interpretada and llm.prompts == []
    assert len(_notas(aid)) == 1


def test_nota_vacia_falla_claro(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    try:
        asyncio.run(notas.anotar(1, aid, "   ", llm=FakeLLM(), today=HOY))
    except ValueError as e:
        assert "vacía" in str(e)
    else:
        raise AssertionError("tenía que fallar")


def test_las_notas_se_mueven_al_fundir(conn: Any) -> None:
    a = asunto("Pasaporte", kind="tramite")
    b = asunto("Sacar el pasaporte", kind="tramite")
    asyncio.run(notas.anotar(1, b, "una nota", llm=FakeLLM(), interpretar=False))
    with connection() as c:
        store.merge_asuntos(c, 1, survivor_id=a, absorbed_id=b)
    assert [n["texto"] for n in _notas(a)] == ["una nota"]


# --------------------------------------------------------------------- API


def test_api_nota_y_story(client: Any, monkeypatch: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    ids = _con_plan(aid)
    llm = FakeLLM(
        _respuesta(
            entendido="Va el viernes.",
            pasos=[{"id": ids["ir"], "fecha": "2026-09-18", "hecho": False}],
        )
    )
    monkeypatch.setattr(notas, "build_llm_client", lambda *a, **k: llm)
    monkeypatch.setattr(notas, "_hoy", lambda: HOY)

    r = client.post(f"/asuntos/{aid}/nota", json={"texto": "el viernes en la tarde voy"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interpretada"] and body["entendido"] == "Va el viernes."
    assert body["cambios"] == ["«Solicitar el pasaporte presencialmente»: viernes 18 sep"]

    s = client.get(f"/asuntos/{aid}").json()
    assert [n["texto"] for n in s["notas"]] == ["el viernes en la tarde voy"]
    assert s["notas"][0]["cambios"] == body["cambios"]
    nota_hitos = [h for h in s["hitos"] if h.get("nota_id") is not None]
    assert len(nota_hitos) == 1
    assert nota_hitos[0]["text"] == "el viernes en la tarde voy"
    assert nota_hitos[0]["inbox_id"] is None and nota_hitos[0]["evidence_id"] is None
    assert nota_hitos[0]["date_is_exact"] is True

    assert client.post("/asuntos/999999/nota", json={"texto": "x"}).status_code == 404
    assert client.post(f"/asuntos/{aid}/nota", json={"texto": " "}).status_code == 422
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_asuntos_notas WHERE asunto_id = :a"), {"a": aid}
        )
    assert n.scalar_one() == 1
