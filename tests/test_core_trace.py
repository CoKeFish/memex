"""core/trace: el tracer (escritura de nodos vía handles) + read_trace (árbol + roll-up de costo).

No usa LLM ni red. El árbol se siembra a mano (create_root/open_module_tracer/handles) y las
llamadas LLM se insertan directo en `llm_calls` para ejercitar el enganche por `inbox_id`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from memex.core.trace import (
    NULL_TRACER,
    attach_to_entity,
    attach_to_root,
    create_root,
    open_module_tracer,
    read_trace,
)
from memex.db import connection

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _seed_inbox(ext: str = "trace") -> int:
    """source + inbox commiteados (la FK `trace_nodes.inbox_id` los necesita)."""
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
            {"n": ext},
        ).scalar_one()
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, :o, CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"s": sid, "e": ext, "o": datetime(2026, 6, 1, tzinfo=UTC)},
        ).scalar_one()
    return int(iid)


def _insert_call(inbox_id: int, purpose: str, cost: str, *, response: str = "") -> int:
    with connection() as c:
        cid = c.execute(
            text(
                """
                INSERT INTO llm_calls
                  (user_id, inbox_id, purpose, model, prompt_tokens, completion_tokens, cost_usd,
                   latency_ms, status, response_text)
                VALUES (1, :i, :p, 'fake', 10, 5, :c, 100, 'ok', :r)
                RETURNING id
                """
            ),
            {"i": inbox_id, "p": purpose, "c": cost, "r": response},
        ).scalar_one()
    return int(cid)


def test_handles_write_nodes_with_hierarchy(conn: Connection) -> None:
    iid = _seed_inbox()
    root_id = create_root(conn, user_id=1, inbox_id=iid, label="msg")
    tracer = open_module_tracer(
        conn, user_id=1, inbox_id=iid, root_id=root_id, slug="finance", label="finance", seq=0
    )
    ent = tracer.entity("mod_finance_transactions", id=42, label="egreso 100 ARS")
    ent.decision("vs tx #7", ref=("mod_finance_transactions", 7), detail={"score": 0.9})
    ent.log("creada nueva")

    rows = (
        conn.execute(
            text(
                "SELECT id, kind, parent_id, module_slug, ref_table, ref_id, detail "
                "FROM trace_nodes WHERE inbox_id = :i ORDER BY id"
            ),
            {"i": iid},
        )
        .mappings()
        .all()
    )
    assert [r["kind"] for r in rows] == ["root", "module", "entity", "decision", "log"]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["module"]["parent_id"] == root_id
    assert by_kind["module"]["module_slug"] == "finance"
    assert by_kind["entity"]["parent_id"] == by_kind["module"]["id"]
    assert (by_kind["entity"]["ref_table"], by_kind["entity"]["ref_id"]) == (
        "mod_finance_transactions",
        42,
    )
    # decision/log cuelgan de la entidad (padres selectivos: se reusó el handle `ent`).
    assert by_kind["decision"]["parent_id"] == by_kind["entity"]["id"]
    assert by_kind["log"]["parent_id"] == by_kind["entity"]["id"]
    assert by_kind["decision"]["ref_id"] == 7
    assert by_kind["decision"]["detail"] == {"score": 0.9}


def test_null_tracer_writes_nothing(conn: Connection) -> None:
    tracer = open_module_tracer(
        conn, user_id=1, inbox_id=999, root_id=None, slug="finance", label="finance", seq=0
    )
    assert tracer is NULL_TRACER
    # Encadenar sobre el no-op no rompe ni escribe.
    tracer.entity("t", id=1, label="x").step("dedup").decision("d")
    assert conn.execute(text("SELECT count(*) FROM trace_nodes")).scalar_one() == 0


def test_create_root_replaces_previous(conn: Connection) -> None:
    iid = _seed_inbox()
    first = create_root(conn, user_id=1, inbox_id=iid, label="a")
    open_module_tracer(
        conn, user_id=1, inbox_id=iid, root_id=first, slug="finance", label="finance", seq=0
    ).entity("t", id=1, label="x")
    second = create_root(conn, user_id=1, inbox_id=iid, label="b")  # delete-then-write
    rows = conn.execute(
        text("SELECT id, kind FROM trace_nodes WHERE inbox_id = :i"), {"i": iid}
    ).all()
    assert [(r[0], r[1]) for r in rows] == [(second, "root")]  # el subárbol viejo se fue


def test_read_trace_none_when_empty() -> None:
    assert read_trace(1, _seed_inbox()) is None


def test_read_trace_attaches_calls_under_root_and_rolls_up_cost() -> None:
    iid = _seed_inbox()
    with connection() as c:
        root_id = create_root(c, user_id=1, inbox_id=iid, label="msg")
        open_module_tracer(
            c, user_id=1, inbox_id=iid, root_id=root_id, slug="finance", label="finance", seq=0
        ).entity("mod_finance_transactions", id=1, label="egreso")
    _insert_call(iid, "module_route", "0.001", response='{"modules":["finance"]}')
    _insert_call(iid, "extract_finance", "0.004", response='{"items":[]}')

    tree = read_trace(1, iid)
    assert tree is not None
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for n in tree:
        by_kind.setdefault(str(n["kind"]), []).append(n)

    # Las dos llm_calls del inbox se cuelgan del root como hojas `llm` sintéticas (label legible).
    assert {str(n["label"]) for n in by_kind["llm"]} == {"Ruteo", "Extracción · finance"}
    assert all(n["parentId"] == root_id for n in by_kind["llm"])
    extract = next(n for n in by_kind["llm"] if n["label"] == "Extracción · finance")
    assert extract["llm"] == {  # payload con el output crudo
        "model": "fake",
        "promptTokens": 10,
        "completionTokens": 5,
        "latencyMs": 100,
        "status": "ok",
        "responseText": '{"items":[]}',
    }
    # Roll-up jerárquico: el root acumula el costo de su subárbol (ruteo + extracción).
    root = by_kind["root"][0]
    assert root["cost"] == {"ownUsd": 0.0, "subtreeUsd": pytest.approx(0.005), "calls": 2}
    # La entidad no tiene LLM debajo (en el slice síncrono el desempate es worker async) → costo 0.
    entity = by_kind["entity"][0]
    assert entity["cost"]["calls"] == 0


def test_read_trace_excludes_calls_from_previous_runs() -> None:
    # Reprocesamiento: el root hace delete-then-write, así que SOLO las llm_calls de la corrida
    # actual (created_at >= root) se cuelgan; las viejas no apilan (sin 2x Ruteo/Extracción).
    iid = _seed_inbox()
    with connection() as c:  # corrida VIEJA: una extracción de hace 1h
        c.execute(
            text(
                """
                INSERT INTO llm_calls
                  (user_id, inbox_id, purpose, model, prompt_tokens, completion_tokens, cost_usd,
                   latency_ms, status, created_at)
                VALUES (1, :i, 'extract_grouped', 'fake', 1, 1, 0.009, 1, 'ok',
                        NOW() - INTERVAL '1 hour')
                """
            ),
            {"i": iid},
        )
    with connection() as c:  # corrida ACTUAL: root nuevo
        create_root(c, user_id=1, inbox_id=iid, label="msg")
    _insert_call(iid, "extract_grouped", "0.004", response='{"finance":[]}')

    tree = read_trace(1, iid)
    assert tree is not None
    llm = [n for n in tree if n["kind"] == "llm"]
    assert len(llm) == 1  # solo la corrida actual; la vieja (hace 1h) queda fuera
    assert llm[0]["cost"]["ownUsd"] == pytest.approx(0.004)


def test_attach_to_entity_hangs_async_desempate_with_null_inbox() -> None:
    # El desempate FASE-2 corre en batch (inbox_id=NULL) y se ata explícito vía node.llm(call_id):
    # read_trace debe traer esa call por id (no por inbox) y subir su costo a la entidad.
    iid = _seed_inbox()
    resp = '{"same": false}'
    with connection() as c:  # call del worker async, SIN inbox_id (como FASE-2)
        cid = int(
            c.execute(
                text(
                    """
                    INSERT INTO llm_calls
                      (user_id, inbox_id, purpose, model, prompt_tokens, completion_tokens,
                       cost_usd, latency_ms, status, response_text)
                    VALUES (1, NULL, 'finance_dedup', 'fake', 1, 1, 0.0008, 1, 'ok', :resp)
                    RETURNING id
                    """
                ),
                {"resp": resp},
            ).scalar_one()
        )
    with connection() as c:
        root = create_root(c, user_id=1, inbox_id=iid, label="msg")
        open_module_tracer(
            c, user_id=1, inbox_id=iid, root_id=root, slug="finance", label="finance", seq=0
        ).entity("mod_finance_transactions", id=7, label="egreso")
        node = attach_to_entity(c, user_id=1, table="mod_finance_transactions", ref_id=7)
        assert node is not None
        node.llm(cid, label="desempate LLM", detail={"same": False})

    tree = read_trace(1, iid)
    assert tree is not None
    desempate = next(n for n in tree if n["kind"] == "llm" and n["label"] == "desempate LLM")
    assert desempate["llmCallId"] == cid
    assert desempate["llm"]["responseText"] == resp  # output crudo de la call con inbox_id NULL
    assert desempate["cost"]["ownUsd"] == pytest.approx(0.0008)
    entity = next(n for n in tree if n["kind"] == "entity")
    assert entity["cost"]["subtreeUsd"] == pytest.approx(0.0008)  # costo sube a la entidad
    assert entity["cost"]["calls"] == 1


def test_attach_to_entity_returns_none_without_node() -> None:
    # Sin nodo de entidad (mensaje batch / no extraído por-mensaje) → None (el worker omite).
    with connection() as c:
        assert attach_to_entity(c, user_id=1, table="mod_finance_transactions", ref_id=999) is None


def test_attach_to_root_hangs_call_under_root_with_null_inbox() -> None:
    # La co-ocurrencia (FASE batch) es per-mensaje pero no produce fila de dominio con `entity`:
    # su call (inbox_id=NULL) se cuelga bajo el ROOT del mensaje vía attach_to_root + node.llm.
    iid = _seed_inbox()
    resp = '{"pairs": []}'
    with connection() as c:  # call del worker, SIN inbox_id (columna), como la co-ocurrencia real
        cid = int(
            c.execute(
                text(
                    """
                    INSERT INTO llm_calls
                      (user_id, inbox_id, purpose, model, prompt_tokens, completion_tokens,
                       cost_usd, latency_ms, status, response_text)
                    VALUES (1, NULL, 'identidades_cooccurrence', 'fake', 1, 1, 0.0005, 1, 'ok', :r)
                    RETURNING id
                    """
                ),
                {"r": resp},
            ).scalar_one()
        )
    with connection() as c:
        root = create_root(c, user_id=1, inbox_id=iid, label="msg")
        node = attach_to_root(c, user_id=1, inbox_id=iid)
        assert node is not None
        node.llm(cid, label="co-ocurrencia", detail={"pairs": 0})

    tree = read_trace(1, iid)
    assert tree is not None
    cooc = next(n for n in tree if n["kind"] == "llm" and n["label"] == "co-ocurrencia")
    assert cooc["parentId"] == root
    assert cooc["llmCallId"] == cid
    assert cooc["cost"]["ownUsd"] == pytest.approx(0.0005)
    root_node = next(n for n in tree if n["kind"] == "root")
    assert root_node["cost"]["subtreeUsd"] == pytest.approx(0.0005)  # el costo sube al root


def test_attach_to_root_returns_none_without_root() -> None:
    # Mensaje sin root (no extraído por-mensaje / batch) → None (el worker omite el atado).
    iid = _seed_inbox()
    with connection() as c:
        assert attach_to_root(c, user_id=1, inbox_id=iid) is None
