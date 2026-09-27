"""El planificador: de un objetivo declarado a pasos concretos, con fuente oficial.

Sin red ni codex: `plan_asunto(ask=...)` reemplaza la llamada por una respuesta fija, que es como
se prueban los curadores. Lo que se verifica acá es el contrato —qué se le exige al modelo y qué se
hace con lo que devuelve—, no la calidad de la búsqueda.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from memex.db import connection
from memex.modules.asuntos import planner, store
from memex.modules.tareas import module as tareas
from tests.asuntos._seed import asunto

_PLAN = {
    "resumen": "Trámite presencial con cita previa; el pasaporte se reclama en la misma sede.",
    "pasos": [
        {
            "titulo": "Pagar el trámite",
            "que_hacer": "Pagá en un banco autorizado o por PSE y guardá el recibo.",
            "donde": "https://cancilleria.gov.co/pasaportes/costos",
            "llevar": ["cédula original"],
            "costo": "COP 195.600",
            "fecha": None,
            "fuente": "https://cancilleria.gov.co/pasaportes/costos",
        },
        {
            "titulo": "Pedir la cita",
            "que_hacer": "Reservá el turno en la página oficial.",
            "donde": "https://citas.cancilleria.gov.co",
            "llevar": [],
            "costo": "",
            "fecha": "2026-11-28",
            "fuente": "https://citas.cancilleria.gov.co",
        },
    ],
    "fuentes": ["https://cancilleria.gov.co/pasaportes"],
    "sin_confirmar": ["Si la sede del norte atiende sábados"],
}


def _ask(payload: dict[str, Any]) -> Any:
    async def ask(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)

    return ask


def _pasos_de(asunto_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            t
            for t in tareas.list_tareas(c, 1, asunto_id=asunto_id, status="all")
            if t.get("parent_id") is not None
        ]


# --------------------------------------------------------------------- el prompt


def test_el_prompt_exige_fuentes_oficiales_y_prohibe_inventar_fechas() -> None:
    """Es el requisito que puso el dueño: sin fuente oficial no se afirma nada."""
    p = planner.build_prompt(
        objetivo="Sacar el pasaporte",
        descripcion="",
        contexto="",
        ciudad="Bogotá, Colombia",
        hoy=date(2026, 9, 15),
    )
    bajo = p.lower()
    assert "oficiales" in bajo and "blogs" in bajo
    assert "sin_confirmar" in p, "lo no confirmado tiene que tener a dónde ir"
    assert "no pongas fechas sugeridas" in bajo
    assert "bogotá" in bajo and "2026-09-15" in p


def test_el_contexto_del_dueno_entra_al_prompt() -> None:
    p = planner.build_prompt(
        objetivo="Sacar el pasaporte",
        descripcion="Primera vez",
        contexto="ya tengo la cédula nueva",
        ciudad="Bogotá, Colombia",
        hoy=date(2026, 9, 15),
    )
    assert "ya tengo la cédula nueva" in p and "Primera vez" in p


# --------------------------------------------------------------------- el parseo


def test_parse_toma_lo_bueno_y_descarta_lo_roto() -> None:
    crudo = json.dumps(
        {
            "resumen": "r",
            "pasos": [
                {"titulo": "", "fuente": "x"},  # sin título: no es un paso
                "no soy un objeto",
                {"titulo": "Pagar", "fecha": "no es fecha", "fuente": "u"},
            ],
            "fuentes": ["a", "", "b"],
            "sin_confirmar": [],
        }
    )
    plan = planner.parse_plan(crudo)
    assert [p.titulo for p in plan.pasos] == ["Pagar"]
    assert plan.pasos[0].fecha is None, "una fecha ilegible no se inventa, se descarta"
    assert plan.fuentes == ("a", "b")


def test_un_json_roto_no_revienta() -> None:
    assert planner.parse_plan("esto no es json").pasos == []


def test_el_tope_de_pasos_se_respeta() -> None:
    muchos = {"resumen": "", "pasos": [{"titulo": f"p{i}"} for i in range(40)]}
    assert len(planner.parse_plan(json.dumps(muchos)).pasos) == planner.MAX_PASOS


# --------------------------------------------------------------------- el resultado


def test_el_plan_queda_como_proyecto_con_pasos(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    res = asyncio.run(planner.plan_asunto(1, aid, ask=_ask(_PLAN), today=date(2026, 9, 15)))
    assert res.creadas == 2 and res.repetidas == 0
    pasos = {t["title"]: t for t in _pasos_de(aid)}
    assert set(pasos) == {"Pagar el trámite", "Pedir la cita"}
    assert all(t["parent_id"] == res.proyecto_id for t in pasos.values())
    # la fecha solo donde la fuente la daba
    assert pasos["Pagar el trámite"]["due_on"] is None
    assert str(pasos["Pedir la cita"]["due_on"]) == "2026-11-28"
    # el paso trae todo lo que hace falta sin abrir nada más
    d = pasos["Pagar el trámite"]["description"]
    assert "COP 195.600" in d and "cédula original" in d and "cancilleria.gov.co" in d


def test_el_resumen_queda_como_estado_del_asunto(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    asyncio.run(planner.plan_asunto(1, aid, ask=_ask(_PLAN), today=date(2026, 9, 15)))
    with connection() as c:
        a = store.get_asunto(c, 1, aid)
    assert a is not None and "cita previa" in a.state_text


def test_correrlo_dos_veces_no_duplica_los_pasos(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    asyncio.run(planner.plan_asunto(1, aid, ask=_ask(_PLAN), today=date(2026, 9, 15)))
    res = asyncio.run(planner.plan_asunto(1, aid, ask=_ask(_PLAN), today=date(2026, 9, 15)))
    assert res.creadas == 0 and res.repetidas == 2
    assert len(_pasos_de(aid)) == 2


def test_dry_run_muestra_el_plan_sin_tocar_nada(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    res = asyncio.run(
        planner.plan_asunto(1, aid, ask=_ask(_PLAN), dry_run=True, today=date(2026, 9, 15))
    )
    assert res.dry_run and len(res.plan.pasos) == 2
    assert res.creadas == 0 and _pasos_de(aid) == []


def test_lo_no_confirmado_llega_al_resultado(conn: Any) -> None:
    aid = asunto("Sacar el pasaporte", kind="tramite")
    res = asyncio.run(planner.plan_asunto(1, aid, ask=_ask(_PLAN), today=date(2026, 9, 15)))
    assert res.plan.sin_confirmar == ("Si la sede del norte atiende sábados",)


def test_un_asunto_inexistente_falla_claro(conn: Any) -> None:
    try:
        asyncio.run(planner.plan_asunto(1, 999999, ask=_ask(_PLAN)))
    except ValueError as e:
        assert "999999" in str(e)
    else:
        raise AssertionError("tenía que fallar")


# --------------------------------------------------------------------- siempre una recomendación


def test_el_prompt_exige_elegir_y_decir_por_que() -> None:
    """Regla del dueño: «quiero que elija por mí lo más conveniente y que sepa por qué». La
    lista de sedes sin elegir es justo lo que no sirve."""
    p = planner.build_prompt(
        objetivo="Sacar el pasaporte",
        descripcion="",
        contexto="",
        ciudad="Bogotá, Colombia",
        hoy=date(2026, 9, 15),
    )
    assert "SIEMPRE UNA RECOMENDACIÓN" in p and "por_que" in p


def test_las_anclas_entran_al_prompt_y_sin_ellas_se_dice_que_se_asume() -> None:
    con = planner.build_prompt(
        objetivo="x",
        descripcion="",
        contexto="",
        ciudad="Bogotá, Colombia",
        hoy=date(2026, 9, 15),
        anclas=[("Casa", "Calle 1 # 2-3, Bogotá"), ("Universidad", "Cra 7 # 40-62")],
    )
    assert "Casa: Calle 1 # 2-3" in con and "Universidad: Cra 7 # 40-62" in con
    sin = planner.build_prompt(
        objetivo="x", descripcion="", contexto="", ciudad="Bogotá, Colombia", hoy=date(2026, 9, 15)
    )
    assert "qué asumiste" in sin, "sin anclas igual recomienda, pero dice qué asumió"


def test_el_por_que_de_la_eleccion_llega_a_la_tarea(conn: Any) -> None:
    plan = {
        "resumen": "r",
        "pasos": [
            {
                "titulo": "Solicitar el pasaporte",
                "que_hacer": "Ir a la sede.",
                "donde": "Sede Centro, Calle 12C # 8-27",
                "llevar": [],
                "costo": "",
                "fecha": None,
                "fuente": "https://cancilleria.gov.co/x",
                "por_que": "La del Centro te queda a la mitad de camino desde la Salesiana; "
                "la Norte (Av 19 # 98-03) atiende igual, sin cita.",
            }
        ],
        "fuentes": [],
        "sin_confirmar": [],
    }
    aid = asunto("Sacar el pasaporte", kind="tramite")
    asyncio.run(planner.plan_asunto(1, aid, ask=_ask(plan), today=date(2026, 9, 15)))
    (paso,) = _pasos_de(aid)
    assert "Por qué esta opción: La del Centro" in paso["description"]


# --------------------------------------------------------------------- lo que pide manda


def test_el_pedido_concreto_del_dueno_manda_sobre_el_nombre_del_asunto() -> None:
    """2026-09-16: el dueño pegó el enlace de una beca («voy a participar») y el plan salió con
    nueve pasos sobre el programa entero, con la beca al final. El contexto concreto es el plan."""
    p = planner.build_prompt(
        objetivo="Programa de Embajadores de Sideral",
        descripcion="",
        contexto="https://ejemplo.org/beca estoy interesado en esto, voy a participar",
        ciudad="Bogotá, Colombia",
        hoy=date(2026, 9, 16),
    )
    assert "LO QUE LA PERSONA PIDE MANDA SOBRE EL NOMBRE DEL ASUNTO" in p
    assert "voy a participar" in p and "un formulario" in p
