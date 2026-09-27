from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.assign import SYSTEM_PROMPT, parse_response, run_assign, run_pending
from memex.modules.asuntos.settings import AsuntosSettings
from tests.asuntos._seed import FakeLLM, asunto, msg, source

_T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_parse_tolerante_a_fences_y_claves_con_nombre() -> None:
    raw = """```json
{"asignaciones":[{"inbox_id":1,"asunto":"A21 · Semillero VCloud","confianza":0.9,"hito":null},
                 {"inbox_id":2,"asunto":"nuevo:1","confianza":0.8,"hito":{"fecha":"2026-03-05","que":"Inicio"}},
                 {"inbox_id":3,"asunto":null,"confianza":0.5,"hito":null},
                 {"inbox_id":4,"asunto":"Semillero","confianza":0.5,"hito":null}],
 "nuevos":[{"ref":"nuevo:1","nombre":"Curso X","tipo":"programa","descripcion":"d","entidades":["PUJ"],"semilla":"registro"}],
 "actualizaciones":[{"asunto":"A21 · Semillero","estado":"en curso","cerrado":false}],
 "relaciones":[{"src":"nuevo:1","dst":"A21","tipo":"parte_de","confianza":0.9},
               {"src":"nuevo:1","dst":"A21","tipo":"rara","confianza":0.9}]}
```"""  # noqa: E501
    p = parse_response(raw)
    assert [a.asunto for a in p.assignments] == ["A21", "nuevo:1", None, None]
    assert (
        p.assignments[1].hito_text == "Inicio" and str(p.assignments[1].hito_date) == "2026-03-05"
    )
    assert p.new[0].kind == "programa" and p.new[0].seed == "registro"
    assert p.updates[0].asunto == "A21"
    assert [(r.src, r.dst, r.relation_type) for r in p.relations] == [
        ("nuevo:1", "A21", "parte_de")
    ]
    assert any(d.startswith("asignacion:clave_invalida") for d in p.dropped)
    assert any(d.startswith("relacion:invalida") for d in p.dropped)
    assert parse_response("no json").dropped == ["json:invalid"]


def _resp(m1: int, m2: int, m3: int, grado: int) -> str:
    return json.dumps(
        {
            "asignaciones": [
                {
                    "inbox_id": m1,
                    "asunto": "nuevo:1",
                    "confianza": 0.9,
                    "hito": {"fecha": "2026-03-02", "que": "Inscripción al examen"},
                },
                {"inbox_id": m2, "asunto": "nuevo:1", "confianza": 0.85, "hito": None},
                {"inbox_id": m3, "asunto": None, "confianza": 0.9, "hito": None},
            ],
            "nuevos": [
                {
                    "ref": "nuevo:1",
                    "nombre": "Examen de inglés 2026-1",
                    "tipo": "tramite",
                    "descripcion": "Clasificación de inglés",
                    "entidades": ["PUJ"],
                    "semilla": "registro",
                }
            ],
            "actualizaciones": [
                {"asunto": "nuevo:1", "estado": "inscrito, examen el 4 de junio", "cerrado": False},
                {"asunto": f"A{grado}", "estado": "solo falta inglés", "cerrado": False},
            ],
            "relaciones": [
                {"src": "nuevo:1", "dst": f"A{grado}", "tipo": "requisito_de", "confianza": 0.9}
            ],
        }
    )


@pytest.mark.asyncio
async def test_run_assign_crea_promueve_hitos_estado_relacion_y_marca_vistos(conn: Any) -> None:
    src = source()
    grado = asunto("Grado", kind="tramite")
    m1 = msg(
        src,
        subject="Examen de clasificación de inglés",
        from_addr="ielec@salesiana.edu.co",
        occurred=_T0,
    )
    m2 = msg(
        src,
        subject="Citación examen inglés",
        from_addr="ielec@salesiana.edu.co",
        occurred=_T0 + timedelta(days=1),
    )
    m3 = msg(src, subject="Newsletter", from_addr="news@x.com", occurred=_T0 + timedelta(days=2))
    m4 = msg(src, subject="no relevante", relevant=False, occurred=_T0)
    llm = FakeLLM(_resp(m1, m2, m3, grado))
    res = await run_assign(1, [m1, m2, m3, m4], client=llm, settings=AsuntosSettings())
    assert res.relevant == 3 and res.llm_batches == 1
    assert res.assigned == 2 and res.none == 1 and res.created == 1 and res.promoted == 1
    assert res.hitos == 1 and res.updates == 2 and res.relations == 1 and res.errors == 0
    assert "## CATÁLOGO" in llm.prompts[0] and f"A{grado}" in llm.prompts[0]
    with connection() as c:
        [nuevo] = [a for a in store.list_asuntos(c, 1) if a.id != grado]
        ev = store.evidence_of(c, 1, nuevo.id)
        rels = store.relations_of(c, 1, grado)
        g = store.get_asunto(c, 1, grado)
        unseen = store.unseen_relevant_inbox_ids(c, 1, limit=10)
    assert nuevo.status == "abierto" and nuevo.evidence_count == 2 and nuevo.kind == "tramite"
    assert nuevo.state_text.startswith("inscrito")
    assert [e.hito_text for e in ev] == ["Inscripción al examen", None]
    assert g is not None and g.state_text == "solo falta inglés"
    assert [(r.relation_type, r.other_id, r.direction) for r in rels] == [
        ("requisito_de", nuevo.id, "in")
    ]
    assert unseen == []  # los 3 relevantes quedaron decididos (m3 sin asunto)

    # segunda corrida: nada que preguntar
    llm2 = FakeLLM()
    res2 = await run_assign(1, [m1, m2, m3], client=llm2, settings=AsuntosSettings())
    assert res2.skipped_seen == 3 and res2.llm_batches == 0 and llm2.prompts == []

    # force: olvida y re-decide (la evidencia vieja se borra; el asunto queda con 0 pero abierto)
    llm3 = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m1, "asunto": f"A{nuevo.id}", "confianza": 0.9, "hito": None},
                    {"inbox_id": m2, "asunto": None, "confianza": 0.9, "hito": None},
                    {"inbox_id": m3, "asunto": None, "confianza": 0.9, "hito": None},
                ],
                "nuevos": [],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    res3 = await run_assign(1, [m1, m2, m3], client=llm3, settings=AsuntosSettings(), force=True)
    assert res3.assigned == 1 and res3.skipped_seen == 0
    with connection() as c:
        assert store.get_asunto(c, 1, nuevo.id).evidence_count == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_adhesion_evita_llm_y_baja_confianza_no_asigna(conn: Any) -> None:
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
    m1 = msg(
        src,
        subject="⏰ Impact Studio Dinner is starting soon",
        from_addr="b@calendar.luma-mail.com",
        occurred=_T0 + timedelta(hours=5),
    )
    m2 = msg(src, subject="Otra cosa", from_addr="c@otro.com", occurred=_T0 + timedelta(hours=6))
    llm2 = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    # el preasignado: el modelo intenta moverlo a null, pero aporta un hito
                    {
                        "inbox_id": m1,
                        "asunto": None,
                        "confianza": 0.9,
                        "hito": {"fecha": "2026-03-01", "que": "Cena del ecosistema"},
                    },
                    {"inbox_id": m2, "asunto": f"A{aid}", "confianza": 0.3, "hito": None},
                ],
                "nuevos": [],
                "actualizaciones": [
                    {"asunto": f"A{aid}", "estado": "cena confirmada", "cerrado": False}
                ],
                "relaciones": [],
            }
        )
    )
    res = await run_assign(1, [m1, m2], client=llm2, settings=AsuntosSettings())
    assert res.adhered == 1 and res.llm_batches == 1 and res.low_confidence == 1 and res.none == 1
    # el adherido SÍ va al LLM, marcado, para hito/estado
    assert f"inbox_id={m1}" in llm2.prompts[0] and f"YA ADHERIDO a A{aid}" in llm2.prompts[0]
    assert res.hitos == 1 and res.updates == 1
    with connection() as c:
        ev = store.evidence_of(c, 1, aid)
        a = store.get_asunto(c, 1, aid)
    assert {(e.inbox_id, e.method) for e in ev} == {(m0, "llm"), (m1, "evento")}
    assert next(e for e in ev if e.inbox_id == m1).hito_text == "Cena del ecosistema"
    assert a is not None and a.state_text == "cena confirmada"


class PoisonLLM(FakeLLM):
    """Rechaza (400) cualquier lote que contenga el mensaje envenenado; el resto lo asigna null."""

    def __init__(self, poison_id: int) -> None:
        super().__init__()
        self.poison = f"inbox_id={poison_id} "
        self.calls = 0

    async def complete(self, messages: Any, **kw: Any) -> Any:
        from memex.llm.client import LLMError

        self.calls += 1
        prompt = messages[-1].content
        if self.poison in prompt:
            raise LLMError(
                400, "client error 400", body='{"error":{"message":"Content Exists Risk"}}'
            )
        ids = [int(x) for x in re.findall(r"inbox_id=(\d+)", prompt)]
        self.responses = [
            json.dumps(
                {
                    "asignaciones": [
                        {"inbox_id": i, "asunto": None, "confianza": 1, "hito": None} for i in ids
                    ],
                    "nuevos": [],
                    "actualizaciones": [],
                    "relaciones": [],
                }
            )
        ]
        return await super().complete(messages, **kw)


@pytest.mark.asyncio
async def test_rechazo_de_contenido_biseca_y_aisla_al_culpable(conn: Any) -> None:
    src = source()
    ids = [msg(src, subject=f"m{i}", occurred=_T0 + timedelta(minutes=i)) for i in range(8)]
    poison = ids[5]
    llm = PoisonLLM(poison)
    res = await run_assign(1, ids, client=llm, settings=AsuntosSettings())
    assert res.rejected == 1 and res.errors == 0 and res.none == 7
    with connection() as c:
        assert store.unseen_relevant_inbox_ids(c, 1, limit=20) == []
        row = c.execute(
            text("SELECT method FROM mod_asuntos_seen WHERE inbox_id = :i"), {"i": poison}
        ).scalar()
    assert row == "rechazado_llm"
    assert 4 <= llm.calls <= 8  # bisección: log2(8)+1 lotes fallidos + los buenos


class TruncatingLLM(FakeLLM):
    """Simula un modelo que razona: con más de `cap` mensajes se queda sin tokens (contenido vacío,
    finish_reason=length); con menos responde bien."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap
        self.calls = 0

    async def complete(self, messages: Any, **kw: Any) -> Any:
        from decimal import Decimal

        from memex.llm.client import LLMResult, LLMUsage

        self.calls += 1
        prompt = messages[-1].content
        ids = [int(x) for x in re.findall(r"inbox_id=(\d+)", prompt)]
        if len(ids) > self.cap:
            return LLMResult(
                content="",
                model="fake-pro",
                usage=LLMUsage(prompt_tokens=10, completion_tokens=8000, total_tokens=8010),
                cost_usd=Decimal("0.01"),
                latency_ms=1,
                finish_reason="length",
            )
        self.responses = [
            json.dumps(
                {
                    "asignaciones": [
                        {"inbox_id": i, "asunto": None, "confianza": 1, "hito": None} for i in ids
                    ],
                    "nuevos": [],
                    "actualizaciones": [],
                    "relaciones": [],
                }
            )
        ]
        return await super().complete(messages, **kw)


@pytest.mark.asyncio
async def test_truncamiento_biseca_y_no_marca_vistos_por_error(conn: Any) -> None:
    src = source()
    ids = [msg(src, subject=f"t{i}", occurred=_T0 + timedelta(minutes=i)) for i in range(8)]
    llm = TruncatingLLM(cap=2)
    res = await run_assign(1, ids, client=llm, settings=AsuntosSettings())
    assert res.none == 8 and res.errors == 0 and res.rejected == 0
    assert res.cost_usd > 0  # los intentos truncados también cuestan y se contabilizan
    with connection() as c:
        assert store.unseen_relevant_inbox_ids(c, 1, limit=20) == []

    # JSON inválido que NO es truncamiento → error de lote, nada queda «visto»
    more = [
        msg(src, subject=f"g{i}", occurred=_T0 + timedelta(hours=1, minutes=i)) for i in range(3)
    ]
    bad = FakeLLM("esto no es json")
    res2 = await run_assign(1, more, client=bad, settings=AsuntosSettings())
    assert res2.errors == 1 and res2.none == 0
    with connection() as c:
        assert store.unseen_relevant_inbox_ids(c, 1, limit=20) == more


@pytest.mark.asyncio
async def test_run_pending_solo_no_vistos_y_apagado(conn: Any) -> None:
    src = source()
    m1 = msg(src, subject="a", occurred=_T0)
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [{"inbox_id": m1, "asunto": None, "confianza": 1, "hito": None}],
                "nuevos": [],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    res = await run_pending(1, client=llm)
    assert res.relevant == 1 and res.none == 1
    res2 = await run_pending(1, client=FakeLLM())
    assert res2.considered == 0
    with connection() as c:
        from memex.modules.asuntos.settings import upsert_settings

        upsert_settings(c, 1, enabled=False)
    m2 = msg(src, subject="b", occurred=_T0)
    res3 = await run_assign(1, [m2], client=FakeLLM())
    assert res3.relevant == 0 and res3.llm_batches == 0


@pytest.mark.asyncio
async def test_run_assign_dry_run_propone_sin_escribir(conn: Any) -> None:
    src = source()
    grado = asunto("Grado", kind="tramite")
    m1 = msg(src, subject="Examen de inglés", from_addr="ielec@salesiana.edu.co", occurred=_T0)
    m2 = msg(src, subject="Citación", from_addr="ielec@salesiana.edu.co", occurred=_T0)
    m3 = msg(src, subject="Newsletter", from_addr="news@x.com", occurred=_T0)
    llm = FakeLLM(_resp(m1, m2, m3, grado))
    res = await run_assign(1, [m1, m2, m3], client=llm, settings=AsuntosSettings(), dry_run=True)
    assert res.llm_batches == 1 and len(res.proposals) == 3
    assert res.created == 0 and res.assigned == 0
    labels = {p["inbox_id"]: p["asunto"] for p in res.proposals}
    assert labels[m1] is not None and labels[m1].startswith("NUEVO ")
    assert labels[m3] is None
    with connection() as c:
        n_asuntos = c.execute(text("SELECT count(*) FROM mod_asuntos")).scalar()
        n_seen = c.execute(text("SELECT count(*) FROM mod_asuntos_seen")).scalar()
        n_ev = c.execute(text("SELECT count(*) FROM mod_asuntos_evidence")).scalar()
    assert (n_asuntos, n_seen, n_ev) == (1, 0, 0)  # nada escrito: solo el «Grado» sembrado


@pytest.mark.asyncio
async def test_run_assign_siembra_estado_de_los_nuevos(conn: Any) -> None:
    src = source()
    grado = asunto("Grado", kind="tramite")
    m1 = msg(src, subject="Examen de inglés", from_addr="ielec@salesiana.edu.co", occurred=_T0)
    m2 = msg(src, subject="Citación", from_addr="ielec@salesiana.edu.co", occurred=_T0)
    m3 = msg(src, subject="Newsletter", from_addr="news@x.com", occurred=_T0)
    await run_assign(
        1, [m1, m2, m3], client=FakeLLM(_resp(m1, m2, m3, grado)), settings=AsuntosSettings()
    )
    with connection() as c:
        empty = c.execute(
            text(
                "SELECT count(*) FROM mod_asuntos WHERE merged_into_id IS NULL "
                "AND id <> :g AND COALESCE(state_text, '') = ''"
            ),
            {"g": grado},
        ).scalar()
    assert empty == 0  # ningún asunto nuevo nace sin estado


def test_parse_toma_el_nombre_nuevo_de_una_actualizacion() -> None:
    p = parse_response(
        json.dumps(
            {
                "asignaciones": [{"inbox_id": 1, "asunto": "A7", "confianza": 0.9, "hito": None}],
                "nuevos": [],
                "actualizaciones": [
                    {"asunto": "A7", "estado": "Tier 2", "cerrado": False, "nombre": "  Y  "},
                    {"asunto": "A7", "estado": "x", "cerrado": False, "nombre": None},
                ],
                "relaciones": [],
            }
        )
    )
    assert [u.new_name for u in p.updates] == ["Y", ""]


def test_prompt_de_chats_dice_que_el_grupo_no_es_el_asunto() -> None:
    # Decisión del dueño (2026-09-17): el chat es el lugar; los asuntos son las iniciativas que
    # ahí se planean (varias por grupo). La regla vive en la sección CHATS DE GRUPO y aplica a
    # los tres modos, incluido «grupo cercano».
    chats = SYSTEM_PROMPT.split("CHATS DE GRUPO", 1)[1]
    assert "EL GRUPO NUNCA ES EL ASUNTO" in chats
    assert "VARIOS asuntos" in chats
    assert "el grupo mismo sigue sin ser asunto" in chats


@pytest.mark.asyncio
async def test_run_assign_renombra_por_cambio_de_nombre_y_deja_alias(conn: Any) -> None:
    # Caso real (2026-09-15): «El espacio que conocías como StarForge pasó a ser el Programa de
    # Embajadores de Sideral». El asunto es el mismo: se adhiere, se renombra y el nombre viejo
    # queda como alias (así el catálogo sigue reconociendo las dos formas).
    src = source()
    viejo = asunto("StarForge Embajadores LATAM")
    m1 = msg(
        src,
        subject="Tu nivel actual dentro del Programa de Embajadores de Sideral",
        from_addr="fib@mail.beehiiv.com",
        body=(
            "El espacio que conocías como StarForge pasó a ser el Programa de Embajadores "
            "de Sideral."
        ),
        occurred=_T0,
    )
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m1, "asunto": f"A{viejo}", "confianza": 0.9, "hito": None}
                ],
                "nuevos": [],
                "actualizaciones": [
                    {
                        "asunto": f"A{viejo}",
                        "estado": "El usuario está en Tier 2 Contributor.",
                        "cerrado": False,
                        "nombre": "Programa de Embajadores de Sideral",
                    }
                ],
                "relaciones": [],
            }
        )
    )
    res = await run_assign(1, [m1], client=llm, settings=AsuntosSettings())
    assert res.assigned == 1 and res.created == 0 and res.renamed == 1
    assert "CAMBIO DE NOMBRE" in SYSTEM_PROMPT and '"nombre":' in SYSTEM_PROMPT
    with connection() as c:
        a = store.get_asunto(c, 1, viejo)
    assert a is not None
    assert a.name == "Programa de Embajadores de Sideral"
    assert "StarForge Embajadores LATAM" in a.aliases
    assert a.state_text.startswith("El usuario está en Tier 2")

    # el mismo nombre (aunque cambie mayúsculas) no cuenta como renombre
    llm2 = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": m1, "asunto": f"A{viejo}", "confianza": 0.9, "hito": None}
                ],
                "nuevos": [],
                "actualizaciones": [
                    {
                        "asunto": f"A{viejo}",
                        "estado": "sin cambios",
                        "cerrado": False,
                        "nombre": "programa de embajadores de sideral",
                    }
                ],
                "relaciones": [],
            }
        )
    )
    res2 = await run_assign(1, [m1], client=llm2, settings=AsuntosSettings(), force=True)
    assert res2.renamed == 0
    with connection() as c:
        a2 = store.get_asunto(c, 1, viejo)
    assert a2 is not None and a2.name == "Programa de Embajadores de Sideral"
