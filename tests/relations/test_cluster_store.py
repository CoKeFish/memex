"""Persistencia de cúmulos: insert idempotente (índice único parcial), memo de rechazo, set-diff que
preserva podados, carga de membresía (incl. podados), dissolved. Refs sintéticos."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.cluster_store import (
    insert_candidate,
    load_clusters,
    mark_dissolved,
    materialize_cluster_edges,
    sync_members,
)
from memex.relations.clustering import CandidateCluster, cluster_signature
from memex.relations.edges import RELTYPE_MIEMBRO_DE, Ref, list_edges

RefT = tuple[str, int]


def _cc(*refs: RefT) -> CandidateCluster:
    members = tuple(sorted((Ref(s, i) for s, i in refs), key=lambda r: (r.slug, r.id)))
    return CandidateCluster(members, cluster_signature(members), False)


def _seed(
    conn: Connection,
    status: str,
    refs: Iterable[RefT],
    *,
    pruned: Iterable[RefT] = (),
) -> int:
    members = frozenset(Ref(s, i) for s, i in refs)
    pruned_set = {Ref(s, i) for s, i in pruned}
    sig = cluster_signature(members)
    cid = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, signature, blob_signature, "
                "member_count) VALUES (1, :st, :sig, :sig, :mc) RETURNING id"
            ),
            {"st": status, "sig": sig, "mc": len(members)},
        ).scalar_one()
    )
    for r in members:
        conn.execute(
            text(
                "INSERT INTO relation_cluster_members "
                "(user_id, cluster_id, member_slug, member_id, pruned) VALUES (1, :c, :s, :i, :p)"
            ),
            {"c": cid, "s": r.slug, "i": r.id, "p": r in pruned_set},
        )
    return cid


def test_insert_idempotente(conn: Connection) -> None:
    cc = _cc(("identidades:person", 1), ("finance", 2), ("calendar", 3))
    id1 = insert_candidate(conn, 1, cc)
    id2 = insert_candidate(conn, 1, cc)
    assert id1 is not None
    assert id2 is None  # misma firma → no duplica
    n = conn.execute(text("SELECT count(*) FROM relation_clusters WHERE user_id = 1")).scalar_one()
    assert n == 1
    m = conn.execute(
        text("SELECT count(*) FROM relation_cluster_members WHERE cluster_id = :c"), {"c": id1}
    ).scalar_one()
    assert m == 3


def test_reject_memo_bloquea_insert(conn: Connection) -> None:
    refs = [("identidades:person", 1), ("finance", 2), ("calendar", 3)]
    _seed(conn, "rejected", refs)
    assert (
        insert_candidate(conn, 1, _cc(*refs)) is None
    )  # firma rechazada → bloqueada por el índice


def test_sync_preserva_podados(conn: Connection) -> None:
    refs = [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)]
    cid = _seed(conn, "confirmed", refs, pruned=[("identidades:person", 3)])
    nuevos = frozenset(Ref(s, i) for s, i in [*refs, ("identidades:person", 4)])
    sync_members(conn, 1, cid, nuevos, cluster_signature(nuevos))
    rows = {
        (str(r["member_slug"]), int(r["member_id"])): bool(r["pruned"])
        for r in conn.execute(
            text(
                "SELECT member_slug, member_id, pruned FROM relation_cluster_members "
                "WHERE cluster_id = :c"
            ),
            {"c": cid},
        ).mappings()
    }
    assert rows[("identidades:person", 3)] is True  # preservó la poda
    assert rows[("identidades:person", 4)] is False  # nuevo, no podado
    mc = conn.execute(
        text("SELECT member_count FROM relation_clusters WHERE id = :c"), {"c": cid}
    ).scalar_one()
    assert mc == 4


def test_sync_borra_idos(conn: Connection) -> None:
    refs = [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)]
    cid = _seed(conn, "confirmed", refs)
    nuevos = frozenset({Ref("identidades:person", 1), Ref("identidades:person", 2)})
    sync_members(conn, 1, cid, nuevos, cluster_signature(nuevos))
    remaining = {
        (str(r["member_slug"]), int(r["member_id"]))
        for r in conn.execute(
            text(
                "SELECT member_slug, member_id FROM relation_cluster_members WHERE cluster_id = :c"
            ),
            {"c": cid},
        ).mappings()
    }
    assert remaining == {("identidades:person", 1), ("identidades:person", 2)}


def test_load_incluye_podados(conn: Connection) -> None:
    refs = [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)]
    _seed(conn, "confirmed", refs, pruned=[("identidades:person", 3)])
    clusters = load_clusters(conn, 1, ("confirmed",))
    assert len(clusters) == 1
    c = clusters[0]
    assert c.members == frozenset(
        Ref(s, i) for s, i in refs
    )  # firma/Jaccard sobre el set detectado
    assert c.pruned == frozenset({Ref("identidades:person", 3)})
    assert c.live_members == frozenset({Ref("identidades:person", 1), Ref("identidades:person", 2)})


def test_mark_dissolved(conn: Connection) -> None:
    cid = _seed(
        conn,
        "confirmed",
        [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)],
    )
    mark_dissolved(conn, 1, cid)
    st = conn.execute(
        text("SELECT status FROM relation_clusters WHERE id = :c"), {"c": cid}
    ).scalar_one()
    assert st == "dissolved"


def _miembro_de(conn: Connection) -> list[tuple[str, int]]:
    return [
        (e.src.slug, e.src.id) for e in list_edges(conn, 1) if e.relation_type == RELTYPE_MIEMBRO_DE
    ]


def test_materialize_solo_confirmed(conn: Connection) -> None:
    _seed(
        conn,
        "candidate",
        [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)],
    )
    assert materialize_cluster_edges(conn, 1) == 0  # un candidate no proyecta aristas
    assert _miembro_de(conn) == []


def test_materialize_gc_remueve_podado(conn: Connection) -> None:
    refs = [("identidades:person", 1), ("identidades:person", 2), ("identidades:person", 3)]
    cid = _seed(conn, "confirmed", refs)
    assert materialize_cluster_edges(conn, 1) == 3
    assert len(_miembro_de(conn)) == 3
    # poda el miembro 3 → al re-materializar, su arista miembro_de se GC-ea (prune no la atraparía)
    conn.execute(
        text(
            "UPDATE relation_cluster_members SET pruned = TRUE "
            "WHERE cluster_id = :c AND member_id = 3"
        ),
        {"c": cid},
    )
    materialize_cluster_edges(conn, 1)
    srcs = _miembro_de(conn)
    assert len(srcs) == 2
    assert ("identidades:person", 3) not in srcs
