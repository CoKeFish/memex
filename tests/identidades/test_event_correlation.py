"""identidades en los eventos del agente: `register_card` con `event_id` deja la MENCIÓN-evento
(la evidencia del avistamiento) y teje incremental las aristas `mismo_evento` con los hechos de
bienestar/finanzas del MISMO evento; sin `event_id` no hay mención. Re-tejer el evento
(`weave_event`) materializa lo mismo desde la mención persistida (idempotente)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import register
from memex.modules.identidades.module import register_card
from memex.relations.deterministic import weave_event
from memex.relations.edges import list_edges

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _mentions(conn: Connection) -> list[tuple[str | None, int | None]]:
    return [
        (r.event_id, r.resolved_identity_id)
        for r in conn.execute(
            text(
                "SELECT event_id, resolved_identity_id FROM mod_identidades_mentions "
                "WHERE user_id = 1 ORDER BY id"
            )
        ).all()
    ]


def test_register_card_con_event_deja_mencion(conn: Connection) -> None:
    row = register_card(conn, 1, name="Juan Niebla", kind="persona", event_id="agent-9")
    mentions = _mentions(conn)
    assert mentions == [("agent-9", int(row["id"]))]


def test_register_card_sin_event_no_deja_mencion(conn: Connection) -> None:
    register_card(conn, 1, name="Juan Niebla", kind="persona")
    assert _mentions(conn) == []


def test_identidad_bienestar_mismo_evento_tejido_en_el_acto(conn: Connection) -> None:
    # el registro de bienestar aterriza primero; la tarjeta (segunda) teje vía weave_event,
    # SIN build_relations: el último en aterrizar crea el par.
    add_habit(conn, 1, name="Gym", cadence="daily", category="ejercicio")
    reg = register(conn, 1, category="ejercicio", activity="gym", event_id="agent-3")
    card = register_card(conn, 1, name="Juan Niebla", kind="persona", event_id="agent-3")
    edges = list_edges(conn, 1, producer="event")
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == "confirmed"
    assert e.relation_type == "mismo_evento"
    assert {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)} == {
        ("bienestar", int(reg["id"])),
        ("identidades:person", int(card["id"])),
    }


def test_weave_event_materializa_desde_mencion(conn: Connection) -> None:
    # la mención-evento persiste → re-tejer el evento re-deriva la arista (idempotente), p.ej. tras
    # una poda; dos menciones de la MISMA identidad+evento no duplican (UNION del CTE).
    add_habit(conn, 1, name="Gym", cadence="daily", category="ejercicio")
    reg = register(conn, 1, category="ejercicio", activity="gym", event_id="agent-4")
    card = register_card(conn, 1, name="Juan Niebla", kind="persona", event_id="agent-4")
    register_card(conn, 1, name="Juan Niebla", kind="persona", event_id="agent-4")  # re-avistada
    conn.execute(text("DELETE FROM relation_edges WHERE user_id = 1"))
    n = weave_event(conn, 1, "agent-4")
    assert n == 1
    edges = list_edges(conn, 1, producer="event")
    assert len(edges) == 1
    assert {(edges[0].src.slug, edges[0].src.id), (edges[0].dst.slug, edges[0].dst.id)} == {
        ("bienestar", int(reg["id"])),
        ("identidades:person", int(card["id"])),
    }
    weave_event(conn, 1, "agent-4")  # re-correr no duplica
    assert len(list_edges(conn, 1, producer="event")) == 1
