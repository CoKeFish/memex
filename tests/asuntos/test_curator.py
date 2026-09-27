from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.curator import (
    collect_items,
    run_curator,
    run_maintenance,
    run_system_rules,
)
from tests.asuntos._seed import asunto, evidence, msg, source


def _age(aid: int, days: int) -> None:
    with connection() as c:
        c.execute(
            text("UPDATE mod_asuntos SET last_signal_at = :t WHERE id = :a"),
            {"a": aid, "t": datetime.now(UTC) - timedelta(days=days)},
        )


def test_collect_items_detecta_pares_candidatos_viejos_y_abiertos_sin_senal(conn: Any) -> None:
    a = asunto("Impulsa Bootcamp 2026")
    b = asunto("Bootcamp Impulsa StarForge")
    c_ = asunto("ruido", status="candidato")
    d = asunto("viejo abierto")
    _age(c_, 20)
    _age(d, 40)
    _age(a, 1)
    _age(b, 1)
    with connection() as c:
        items = collect_items(c, 1, scope="all", limit=50, inactive_days=60)
    keys = {it.key for it in items}
    assert f"pair:{min(a, b)}-{max(a, b)}" in keys
    assert f"asunto:{c_}" in keys and f"asunto:{d}" in keys


@pytest.mark.asyncio
async def test_run_curator_aplica_merge_rename_close_relation_y_delete(conn: Any) -> None:
    src = source()
    a = asunto("Impulsa Bootcamp 2026")
    b = asunto("Bootcamp Impulsa StarForge")
    ing = asunto("Examen de inglés", kind="tramite")
    gr = asunto("Grado", kind="tramite")
    ruido = asunto("Recibo suelto", status="candidato")
    evidence(a, msg(src, subject="1"))
    evidence(b, msg(src, subject="2"))
    evidence(ruido, msg(src, subject="3"))
    for x in (a, b, ing, gr, ruido):
        _age(x, 30)
    with connection() as c:
        store.update_fields(c, 1, ing, entities=["PUJ", "Carrera"])
        store.update_fields(c, 1, gr, entities=["PUJ", "Carrera"])

    async def ask(prompt: str) -> str:
        assert "item=pair:" in prompt
        return json.dumps(
            {
                "decisions": [
                    {
                        "item": f"pair:{min(a, b)}-{max(a, b)}",
                        "action": "merge",
                        "confidence": 0.95,
                        "rationale": "mismo bootcamp",
                        "asunto_id": a,
                        "other_id": b,
                        "name": None,
                        "state": None,
                        "relation_type": None,
                        "kind": None,
                    },
                    {
                        "item": f"asunto:{a}",
                        "action": "rename",
                        "confidence": 0.9,
                        "rationale": "",
                        "asunto_id": a,
                        "other_id": None,
                        "name": "Impulsa Bootcamp (StarForge) 2026",
                        "state": None,
                        "relation_type": None,
                        "kind": None,
                    },
                    {
                        "item": f"asunto:{gr}",
                        "action": "close",
                        "confidence": 0.9,
                        "rationale": "",
                        "asunto_id": gr,
                        "other_id": None,
                        "name": None,
                        "state": "graduado",
                        "relation_type": None,
                        "kind": None,
                    },
                    {
                        "item": f"pair:{min(ing, gr)}-{max(ing, gr)}",
                        "action": "set_relation",
                        "confidence": 0.9,
                        "rationale": "",
                        "asunto_id": ing,
                        "other_id": gr,
                        "name": None,
                        "state": None,
                        "relation_type": "requisito_de",
                        "kind": None,
                    },
                    {
                        "item": f"asunto:{ruido}",
                        "action": "delete",
                        "confidence": 0.9,
                        "rationale": "",
                        "asunto_id": ruido,
                        "other_id": None,
                        "name": None,
                        "state": None,
                        "relation_type": None,
                        "kind": None,
                    },
                    {
                        "item": f"asunto:{ing}",
                        "action": "set_kind",
                        "confidence": 0.5,
                        "rationale": "",
                        "asunto_id": ing,
                        "other_id": None,
                        "name": None,
                        "state": None,
                        "relation_type": None,
                        "kind": "programa",
                    },
                    {
                        "item": "asunto:999999",
                        "action": "close",
                        "confidence": 0.9,
                        "rationale": "",
                        "asunto_id": 999999,
                        "other_id": None,
                        "name": None,
                        "state": None,
                        "relation_type": None,
                        "kind": None,
                    },
                ]
            }
        )

    st = await run_curator(1, scope="all", ask=ask)
    assert st.calls == 1 and st.errors == 0
    assert st.merged == 1 and st.renamed == 1 and st.closed == 1 and st.related == 1
    assert st.deleted == 1 and st.skipped == 1 and st.dropped == 1
    with connection() as c:
        sa = store.get_asunto(c, 1, a)
        assert sa is not None and sa.name.startswith("Impulsa Bootcamp (StarForge)")
        assert "Bootcamp Impulsa StarForge" in sa.aliases and "Impulsa Bootcamp 2026" in sa.aliases
        assert store.get_asunto(c, 1, b).merged_into_id == a  # type: ignore[union-attr]
        assert store.get_asunto(c, 1, gr).status == "cerrado"  # type: ignore[union-attr]
        assert store.get_asunto(c, 1, ruido) is None
        assert store.get_asunto(c, 1, ing).kind == "tramite"  # type: ignore[union-attr]
        rels = store.relations_of(c, 1, ing)
    assert [(r.relation_type, r.other_id) for r in rels] == [("requisito_de", gr)]


@pytest.mark.asyncio
async def test_curador_revisa_relaciones_del_asignador(conn: Any) -> None:
    ing = asunto("Examen de inglés", kind="tramite")
    ret = asunto("Retiro de asignaturas", kind="tramite")
    gr = asunto("Grado", kind="tramite")
    with connection() as c:
        store.add_relation(c, 1, ing, ret, "requisito_de", source="llm")  # falsa
        store.add_relation(c, 1, ing, gr, "requisito_de", source="llm")  # correcta
        rels = store.llm_relations(c, 1)
    assert len(rels) == 2
    bad = next(r for r in rels if r[2] == ret)[0]
    good = next(r for r in rels if r[2] == gr)[0]

    async def ask(prompt: str) -> str:
        assert f"item=rel:{bad}" in prompt and f"item=rel:{good}" in prompt
        base = {
            "asunto_id": None,
            "other_id": None,
            "name": None,
            "state": None,
            "relation_type": None,
            "kind": None,
            "rationale": "",
        }
        return json.dumps(
            {
                "decisions": [
                    {**base, "item": f"rel:{bad}", "action": "reject_relation", "confidence": 0.9},
                    {**base, "item": f"rel:{good}", "action": "keep", "confidence": 0.9},
                ]
            }
        )

    st = await run_curator(1, scope="relations", ask=ask)
    assert st.relations_rejected == 1 and st.kept == 1
    with connection() as c:
        left = store.relations_of(c, 1, ing)
    assert [(r.relation_type, r.other_id) for r in left] == [("requisito_de", gr)]


@pytest.mark.asyncio
async def test_curador_nombres_evidencia_e_inactivos(conn: Any) -> None:
    src = source()
    blk = asunto("Blockville – Sideral Summer Friday v18.0", kind="evento")  # noqa: RUF001
    dorm = asunto("Dormido", status="inactivo")
    evidence(blk, msg(src, subject="v18"))
    evidence(blk, msg(src, subject="v19"))
    baja = msg(src, subject="Newsletter suelta", from_addr="news@x.com")
    with connection() as c:
        store.add_evidence(c, 1, blk, baja, method="llm", confidence=0.5)
        [ev] = [e for e in store.evidence_of(c, 1, blk) if e.inbox_id == baja]
        items = collect_items(c, 1, scope="all", limit=50, inactive_days=60)
    keys = {it.key for it in items}
    assert f"name:{blk}" in keys and f"ev:{ev.id}" in keys and f"asunto:{dorm}" in keys
    ev_item = next(it for it in items if it.key == f"ev:{ev.id}")
    assert "Newsletter suelta" in ev_item.extra

    async def ask(prompt: str) -> str:
        assert "MENSAJE #" in prompt and "revisar nombre y tipo" in prompt
        base = {
            "asunto_id": None,
            "other_id": None,
            "name": None,
            "state": None,
            "relation_type": None,
            "kind": None,
            "rationale": "",
        }
        return json.dumps(
            {
                "decisions": [
                    {
                        **base,
                        "item": f"name:{blk}",
                        "action": "rename",
                        "confidence": 0.9,
                        "asunto_id": blk,
                        "name": "Sideral Summer Fridays (Blockville)",
                        "kind": "comunidad",
                    },
                    {**base, "item": f"ev:{ev.id}", "action": "reject_evidence", "confidence": 0.9},
                    {
                        **base,
                        "item": f"asunto:{dorm}",
                        "action": "keep",
                        "confidence": 0.8,
                        "asunto_id": dorm,
                    },
                ]
            }
        )

    st = await run_curator(1, scope="all", ask=ask)
    assert st.renamed == 1 and st.evidence_rejected == 1 and st.kept == 1 and st.errors == 0
    with connection() as c:
        a = store.get_asunto(c, 1, blk)
        assert a is not None and a.name.startswith("Sideral Summer Fridays")
        assert a.kind == "comunidad"
        assert "Blockville – Sideral Summer Friday v18.0" in a.aliases  # noqa: RUF001
        assert store.is_rejected(c, 1, blk, baja)
        # incremental: nada de esto vuelve a entrar
        keys2 = {it.key for it in collect_items(c, 1, scope="all", limit=50, inactive_days=60)}
    assert not ({f"name:{blk}", f"ev:{ev.id}", f"asunto:{dorm}"} & keys2)


@pytest.mark.asyncio
async def test_curador_relaciona_contra_el_indice(conn: Any) -> None:
    src = source()
    g = asunto("Proceso de grado", kind="tramite")
    i = asunto("Examen de inglés", kind="tramite")
    col = asunto("Intensivo de inglés Britano", kind="programa")
    for x in (g, i, col):
        evidence(x, msg(src, subject=f"m{x}a"))
        evidence(x, msg(src, subject=f"m{x}b"))

    async def ask(prompt: str) -> str:
        assert (
            "## ÍNDICE DEL CATÁLOGO" in prompt and f"A{col} · Intensivo de inglés Britano" in prompt
        )
        base = {
            "asunto_id": None,
            "other_id": None,
            "name": None,
            "state": None,
            "relation_type": None,
            "kind": None,
            "rationale": "",
        }
        return json.dumps(
            {
                "decisions": [
                    {
                        **base,
                        "item": f"relq:{i}",
                        "action": "set_relation",
                        "confidence": 0.9,
                        "asunto_id": i,
                        "other_id": g,
                        "relation_type": "requisito_de",
                    },
                    {
                        **base,
                        "item": f"relq:{i}",
                        "action": "set_relation",
                        "confidence": 0.9,
                        "asunto_id": col,
                        "other_id": i,
                        "relation_type": "requisito_de",
                    },
                    {**base, "item": f"relq:{g}", "action": "keep", "confidence": 0.9},
                    {
                        **base,
                        "item": f"relq:{col}",
                        "action": "set_relation",
                        "confidence": 0.9,
                        "asunto_id": g,
                        "other_id": i,
                        "relation_type": "parte_de",
                    },  # ni src ni dst = ítem
                ]
            }
        )

    st = await run_curator(1, scope="relations", ask=ask)
    assert st.related == 2 and st.kept == 1 and st.dropped == 1 and st.errors == 0
    with connection() as c:
        rels = {(r.relation_type, r.other_id, r.direction) for r in store.relations_of(c, 1, i)}
        assert rels == {("requisito_de", g, "out"), ("requisito_de", col, "in")}
        assert [a.id for a in store.unreviewed_relations(c, 1)] == [col]  # el drop no marca


def test_system_rules_inactividad_y_reactivacion(conn: Any) -> None:
    src = source()
    a = asunto("viejo")
    b = asunto("dormido", status="inactivo")
    cand = asunto("cand", status="candidato")
    evidence(cand, msg(src, subject="1"))
    evidence(cand, msg(src, subject="2"))
    _age(a, 90)
    _age(b, 1)
    with connection() as c:
        st = run_system_rules(c, 1)
        assert st.inactivated == 1 and st.reactivated == 1 and st.promoted == 1
        assert store.get_asunto(c, 1, a).status == "inactivo"  # type: ignore[union-attr]
        assert store.get_asunto(c, 1, b).status == "abierto"  # type: ignore[union-attr]
        assert store.get_asunto(c, 1, cand).status == "abierto"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_maintenance_escribe_marca(conn: Any) -> None:
    async def ask(prompt: str) -> str:
        return json.dumps({"decisions": []})

    st = await run_maintenance(1, ask=ask)
    assert st.errors == 0
    from memex.modules.asuntos.curator import get_marks

    with connection() as c:
        assert get_marks(c, 1)["last_run_at"] is not None


def test_collect_items_empareja_candidato_por_token_distintivo(conn: Any) -> None:
    """Un candidato que comparte un token distintivo («blockchain») con un asunto cerrado del
    mismo hilo entra como par aunque el Jaccard de nombres sea bajo."""
    from memex.modules.asuntos.curator import collect_items

    old = asunto("Sesiones Blockchain (Salesiana)", kind="otro", status="cerrado")
    cand = asunto("Curso Blockchain y criptoactivos 2026-1", kind="otro", status="candidato")
    other = asunto("Curso Creatividad", kind="otro", status="candidato")
    with connection() as c:
        c.execute(text("UPDATE mod_asuntos SET closed_at = NOW() WHERE id = :i"), {"i": old})
        items = collect_items(c, 1, scope="duplicates", limit=50, inactive_days=60)
    keys = {i.key: i.reason for i in items}
    assert f"pair:{old}-{cand}" in keys and "blockchain" in keys[f"pair:{old}-{cand}"]
    assert f"pair:{cand}-{other}" not in keys  # «curso» es corto: no es distintivo


def test_collect_items_empareja_por_dos_tokens_distintivos(conn: Any) -> None:
    """«Impulsa Bootcamp 2026» [cerrado] y «Impulsa Bootcamp Satélite Bogotá S03» [abierto]: Jaccard
    bajo por los tokens extra, pero dos tokens distintivos en común → par para el curador."""
    from memex.modules.asuntos.curator import collect_items

    prog = asunto("Impulsa Bootcamp 2026", kind="programa", status="cerrado")
    sess = asunto("Impulsa Bootcamp Satélite Bogotá S03", kind="evento", status="abierto")
    other = asunto("Bootcamp de Datos", kind="programa", status="abierto")  # solo 1 token
    with connection() as c:
        c.execute(text("UPDATE mod_asuntos SET closed_at = NOW() WHERE id = :i"), {"i": prog})
        items = collect_items(c, 1, scope="duplicates", limit=50, inactive_days=60)
    keys = {i.key: i.reason for i in items}
    assert f"pair:{prog}-{sess}" in keys and "bootcamp" in keys[f"pair:{prog}-{sess}"]
    assert f"pair:{prog}-{other}" not in keys and f"pair:{sess}-{other}" not in keys


@pytest.mark.asyncio
async def test_maintenance_siembra_estados_faltantes(conn: Any) -> None:
    async def ask(prompt: str) -> str:
        return json.dumps({"decisions": []})

    a = asunto("Semillero", kind="comunidad", status="abierto")
    with connection() as c:
        c.execute(
            text("UPDATE mod_asuntos SET description = 'Semillero de robótica' WHERE id = :i"),
            {"i": a},
        )
    st = await run_maintenance(1, ask=ask)
    assert st.seeded == 1
    with connection() as c:
        assert (
            c.execute(text("SELECT state_text FROM mod_asuntos WHERE id = :i"), {"i": a}).scalar()
            == "Semillero de robótica"
        )
