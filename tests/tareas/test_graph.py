"""tareas en el grafo: vértice `tarea` (las canceladas no), arista «parte_de» tarea→asunto tejida
al crear/re-ligar, y el productor mismo-evento conecta con otros hechos por `event_id`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import register
from memex.modules.tareas.module import add, set_status, update
from memex.relations.deterministic import weave_all_reales
from memex.relations.edges import PRODUCER_EVENT, PRODUCER_TAREAS, list_edges
from memex.relations.vertices import list_vertices

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _asunto(conn: Connection, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status) "
                "VALUES (1, :n, 'programa', 'abierto') RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def test_tarea_is_vertex_unless_cancelled(conn: Connection) -> None:
    t = add(conn, 1, title="Cambiar EPS")
    verts = {v.id: v for v in list_vertices(conn, 1, slugs=("tarea",))}
    assert verts[t["id"]].kind == "tarea" and verts[t["id"]].label == "Cambiar EPS"
    set_status(conn, 1, t["id"], "cancelada")
    assert t["id"] not in {v.id for v in list_vertices(conn, 1, slugs=("tarea",))}


def test_parte_de_edge_woven_on_add_and_update(conn: Connection) -> None:
    britano = _asunto(conn, "Britano")
    ingles = _asunto(conn, "Inglés")
    t = add(conn, 1, title="Ejercicios 42", asunto=britano)
    edges = [e for e in list_edges(conn, 1, producer=PRODUCER_TAREAS)]
    assert len(edges) == 1
    e = edges[0]
    assert (e.src.slug, e.src.id, e.dst.slug, e.dst.id) == ("tarea", t["id"], "asunto", britano)
    assert e.relation_type == "parte_de" and e.verdict == "confirmed"
    update(conn, 1, t["id"], asunto=ingles)
    edges = [e for e in list_edges(conn, 1, producer=PRODUCER_TAREAS)]
    assert [(x.dst.id) for x in edges] == [ingles]  # la vieja se fue con el vértice
    # reparación (full-sweep) es idempotente
    assert weave_all_reales(conn, 1)["tareas"] == 1
    assert len(list_edges(conn, 1, producer=PRODUCER_TAREAS)) == 1


def test_same_event_connects_tarea_with_other_facts(conn: Connection) -> None:
    add_habit(conn, 1, name="gym", cadence="daily", activity="gimnasio")
    r = register(conn, 1, category="ejercicio", activity="gimnasio", event_id="E9")
    t = add(conn, 1, title="Comprar tenis", event_id="E9")
    add(conn, 1, title="Otra cosa")  # sin evento
    pairs = {
        frozenset({(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)})
        for e in list_edges(conn, 1, producer=PRODUCER_EVENT)
    }
    assert frozenset({("bienestar", int(r["id"])), ("tarea", t["id"])}) in pairs
    assert len(pairs) == 1
