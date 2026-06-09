"""Cronología de un cúmulo (`relations.timeline`): sucesos fechados ordenados cronológicamente +
elenco (miembros sin fecha) separado; hackatón sin fecha → elenco; 404 (None) si no es confirmed."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.clustering import cluster_signature
from memex.relations.edges import Ref
from memex.relations.timeline import cluster_timeline


def _confirmed_cluster(conn: Connection, members: list[tuple[str, int]]) -> int:
    refs = [Ref(s, i) for s, i in members]
    sig = cluster_signature(refs)
    cid = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, name, description, confidence, "
                "member_count, signature, blob_signature, validated_signature) "
                "VALUES (1, 'confirmed', 'Mi contexto', 'sinopsis', 0.9, :mc, :sig, :sig, :sig) "
                "RETURNING id"
            ),
            {"mc": len(refs), "sig": sig},
        ).scalar_one()
    )
    for s, i in members:
        conn.execute(
            text(
                "INSERT INTO relation_cluster_members "
                "(user_id, cluster_id, member_slug, member_id) VALUES (1, :c, :s, :i)"
            ),
            {"c": cid, "s": s, "i": i},
        )
    return cid


def test_timeline_ordena_sucesos_y_separa_elenco(conn: Connection) -> None:
    fin = int(
        conn.execute(
            text(
                "INSERT INTO mod_finance_consolidated (user_id, direction, amount, currency, "
                "occurred_at, occurred_at_precision, counterparty) "
                "VALUES (1, 'egreso', 100, 'COP', TIMESTAMPTZ '2026-03-15 14:00:00-05', "
                "'datetime', 'Uber') RETURNING id"
            )
        ).scalar_one()
    )
    cal = int(
        conn.execute(
            text(
                "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on, start_time) "
                "VALUES (1, 'Reunión', DATE '2026-02-01', TIME '09:00') RETURNING id"
            )
        ).scalar_one()
    )
    bien = int(
        conn.execute(
            text(
                "INSERT INTO mod_bienestar_registros (user_id, category, activity, occurred_at) "
                "VALUES (1, 'comida', 'almuerzo', TIMESTAMPTZ '2026-01-10 08:00:00-05') "
                "RETURNING id"
            )
        ).scalar_one()
    )
    per = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, 'persona', 'Ana') RETURNING id"
            )
        ).scalar_one()
    )
    cid = _confirmed_cluster(
        conn,
        [("finance", fin), ("calendar", cal), ("bienestar", bien), ("identidades:person", per)],
    )

    tl = cluster_timeline(conn, 1, cid)
    assert tl is not None
    assert tl.cluster.name == "Mi contexto" and tl.cluster.description == "sinopsis"
    # cronológico: bienestar (10 ene) < calendar (1 feb) < finance (15 mar)
    assert [e.slug for e in tl.events] == ["bienestar", "calendar", "finance"]
    assert [e.label for e in tl.events] == ["almuerzo", "Reunión", "Uber"]
    cal_ev = next(e for e in tl.events if e.slug == "calendar")
    assert cal_ev.precision == "datetime" and "T09:00" in cal_ev.at
    # elenco: la persona (sin fecha de evento)
    assert [(a.slug, a.label) for a in tl.actors] == [("identidades:person", "Ana")]


def test_timeline_hackaton_sin_fecha_va_al_elenco(conn: Connection) -> None:
    hk = int(
        conn.execute(
            text(
                "INSERT INTO mod_hackathones_events (user_id, source_inbox_ids, name) "
                "VALUES (1, '{}', 'HackX') RETURNING id"  # starts_on NULL
            )
        ).scalar_one()
    )
    cid = _confirmed_cluster(conn, [("hackathones", hk)])
    tl = cluster_timeline(conn, 1, cid)
    assert tl is not None
    assert tl.events == []
    assert [(a.slug, a.label) for a in tl.actors] == [("hackathones", "HackX")]


def test_timeline_none_si_no_confirmed(conn: Connection) -> None:
    sig = cluster_signature([Ref("identidades:person", 1)])
    cid = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, signature, blob_signature, "
                "member_count) VALUES (1, 'candidate', :sig, :sig, 1) RETURNING id"
            ),
            {"sig": sig},
        ).scalar_one()
    )
    assert cluster_timeline(conn, 1, cid) is None  # candidate, no confirmed
    assert cluster_timeline(conn, 1, 999999) is None  # inexistente
