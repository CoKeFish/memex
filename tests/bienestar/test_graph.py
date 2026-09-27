"""bienestar en el grafo: es vértice (`list_vertices`) y el productor mismo-evento conecta por
`event_id` (status confirmed); sin event compartido no hay arista. Además: `register` teje la arista
en el acto (incremental, sin full-sweep) y `weave_event` acota a un solo evento."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import register
from memex.relations.deterministic import weave_event
from memex.relations.edges import list_edges
from memex.relations.vertices import list_vertices

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _seed_habits(conn: Connection) -> None:
    """bienestar es para hábitos: registrar exige un hábito activo que cubra la categoría. (Las
    aristas `cumple` que esto teje no afectan a los tests: filtran por producer='event'/slug.)"""
    for cat in ("comida", "higiene", "ejercicio", "grooming", "salud", "otros"):
        add_habit(conn, 1, name=cat, cadence="daily", category=cat)


def test_bienestar_is_vertex(conn: Connection) -> None:
    _seed_habits(conn)
    r = register(conn, 1, category="comida", activity="almuerzo")
    verts = list_vertices(conn, 1, slugs=("bienestar",))
    v = next(v for v in verts if v.id == int(r["id"]))
    assert v.slug == "bienestar"
    assert v.label == "almuerzo"  # label = activity (o category si vacía)


def test_event_id_stored(conn: Connection) -> None:
    _seed_habits(conn)
    a = register(conn, 1, category="comida", activity="almuerzo", event_id="E1")
    b = register(conn, 1, category="higiene", activity="cepillado")
    assert a["event_id"] == "E1"
    assert b["event_id"] is None


def test_same_event_producer_connects(conn: Connection) -> None:
    _seed_habits(conn)
    a = register(conn, 1, category="comida", activity="almuerzo", event_id="E1")
    b = register(conn, 1, category="ejercicio", activity="caminata", event_id="E1")
    c = register(conn, 1, category="higiene", activity="cepillado", event_id="E2")  # otro evento
    d = register(conn, 1, category="salud", activity="ibuprofeno")  # sin event

    # `register` teje «mismo_evento» en el acto (paso 5): a↔b (E1) ya existe sin full-sweep.
    edges = list_edges(conn, 1, producer="event")
    pairs = {(e.src.id, e.dst.id) for e in edges}
    assert (int(a["id"]), int(b["id"])) in pairs  # mismo evento E1 → conectados
    # c (evento solo) y d (sin evento) no comparten evento con nadie → ninguna arista los toca.
    touched = {x for e in edges for x in (e.src.id, e.dst.id)}
    assert int(c["id"]) not in touched
    assert int(d["id"]) not in touched
    for e in edges:
        assert e.verdict == "confirmed"
        assert e.relation_type == "mismo_evento"
        assert e.src.slug == "bienestar"
        assert e.dst.slug == "bienestar"


def test_same_event_idempotent(conn: Connection) -> None:
    _seed_habits(conn)
    register(conn, 1, category="comida", activity="almuerzo", event_id="E1")
    register(conn, 1, category="ejercicio", activity="caminata", event_id="E1")
    weave_event(conn, 1, "E1")  # re-tejer el mismo evento no duplica
    weave_event(conn, 1, "E1")
    assert len(list_edges(conn, 1, producer="event")) == 1


def test_same_event_woven_on_register(conn: Connection) -> None:
    # registrar el segundo hecho del mismo evento teje la arista en el acto, SIN build_relations.
    _seed_habits(conn)
    a = register(conn, 1, category="comida", activity="almuerzo", event_id="E1")
    b = register(conn, 1, category="ejercicio", activity="caminata", event_id="E1")
    edges = list_edges(conn, 1, producer="event")
    pairs = {(e.src.id, e.dst.id) for e in edges}
    assert (int(a["id"]), int(b["id"])) in pairs


def test_weave_event_scoped(conn: Connection) -> None:
    # weave_event acota a UN evento: no toca los pares de otros eventos.
    _seed_habits(conn)
    a = register(conn, 1, category="comida", activity="almuerzo", event_id="E1")
    b = register(conn, 1, category="ejercicio", activity="caminata", event_id="E1")
    register(conn, 1, category="higiene", activity="cepillado", event_id="E2")
    register(conn, 1, category="salud", activity="vitamina", event_id="E2")
    conn.execute(text("DELETE FROM relation_edges WHERE user_id = 1"))  # aislar el productor
    n = weave_event(conn, 1, "E1")
    assert n == 1
    edges = list_edges(conn, 1, producer="event")
    assert len(edges) == 1  # solo el par de E1
    assert {edges[0].src.id, edges[0].dst.id} == {int(a["id"]), int(b["id"])}
