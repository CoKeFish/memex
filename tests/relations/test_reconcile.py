"""Reconciliación de cúmulos: nuevo candidato, idempotencia (firma igual → touch), deriva (sync +
needs_revalidation por umbral), candidato sin match borrado, confirmed sin match disuelto, memo de
rechazo (exacto y difuso), identidad a un confirmed (no duplica). Refs sintéticos + seed directo."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.cluster_store import load_clusters
from memex.relations.clustering import CandidateCluster, cluster_signature
from memex.relations.edges import Ref
from memex.relations.reconcile import reconcile_clusters

RefT = tuple[str, int]


def _cc(*refs: RefT) -> CandidateCluster:
    members = tuple(sorted((Ref(s, i) for s, i in refs), key=lambda r: (r.slug, r.id)))
    return CandidateCluster(members, cluster_signature(members), False)


def _persons(*ids: int) -> list[RefT]:
    return [("identidades:person", i) for i in ids]


def _seed(conn: Connection, status: str, refs: Iterable[RefT]) -> int:
    members = frozenset(Ref(s, i) for s, i in refs)
    sig = cluster_signature(members)
    cid = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, signature, member_count) "
                "VALUES (1, :st, :sig, :mc) RETURNING id"
            ),
            {"st": status, "sig": sig, "mc": len(members)},
        ).scalar_one()
    )
    for r in members:
        conn.execute(
            text(
                "INSERT INTO relation_cluster_members "
                "(user_id, cluster_id, member_slug, member_id) VALUES (1, :c, :s, :i)"
            ),
            {"c": cid, "s": r.slug, "i": r.id},
        )
    return cid


def _status_of(conn: Connection, cid: int) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM relation_clusters WHERE id = :c"), {"c": cid}
        ).scalar_one()
    )


def _needs_reval(conn: Connection, cid: int) -> bool:
    return bool(
        conn.execute(
            text("SELECT needs_revalidation FROM relation_clusters WHERE id = :c"), {"c": cid}
        ).scalar_one()
    )


def test_nuevo_candidato(conn: Connection) -> None:
    cc = _cc(*_persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.new_candidates == 1
    [c] = load_clusters(conn, 1, ("candidate",))
    assert c.members == cc.member_set


def test_idempotente_firma_igual(conn: Connection) -> None:
    cc = _cc(*_persons(1, 2, 3))
    reconcile_clusters(conn, 1, [cc])
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.matched_same == 1
    assert stats.new_candidates == 0
    n = conn.execute(text("SELECT count(*) FROM relation_clusters WHERE user_id = 1")).scalar_one()
    assert n == 1


def test_deriva_sync(conn: Connection) -> None:
    reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    cc2 = _cc(*_persons(1, 2, 3, 4))  # Jaccard 3/4 = 0.75 ≥ 0.5 → match con deriva
    stats = reconcile_clusters(conn, 1, [cc2])
    assert stats.matched_drift == 1
    [c] = load_clusters(conn, 1, ("candidate",))
    assert c.members == cc2.member_set
    assert c.signature == cc2.signature


def test_candidato_sin_match_borrado(conn: Connection) -> None:
    reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    stats = reconcile_clusters(conn, 1, [])
    assert stats.deleted == 1
    assert load_clusters(conn, 1, ("candidate",)) == []


def test_confirmed_sin_match_disuelto(conn: Connection) -> None:
    cid = _seed(conn, "confirmed", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [])
    assert stats.dissolved == 1
    assert _status_of(conn, cid) == "dissolved"


def test_confirmed_deriva_chica_no_revalida(conn: Connection) -> None:
    cid = _seed(conn, "confirmed", _persons(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
    cc = _cc(*_persons(1, 2, 3, 4, 5, 6, 7, 8, 9))  # Jaccard 9/10 = 0.9 = stable → NO revalida
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.matched_drift == 1
    assert _needs_reval(conn, cid) is False


def test_confirmed_deriva_grande_revalida(conn: Connection) -> None:
    cid = _seed(conn, "confirmed", _persons(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
    cc = _cc(*_persons(1, 2, 3, 4, 5, 6))  # Jaccard 6/10 = 0.6 (< 0.9, ≥ 0.5) → revalida
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.matched_drift == 1
    assert _needs_reval(conn, cid) is True


def test_identico_a_confirmed_no_duplica(conn: Connection) -> None:
    _seed(conn, "confirmed", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    assert stats.matched_same == 1
    assert stats.new_candidates == 0
    rows = (
        conn.execute(text("SELECT status FROM relation_clusters WHERE user_id = 1")).scalars().all()
    )
    assert rows == ["confirmed"]


def test_memo_rechazo_exacto_suprime(conn: Connection) -> None:
    refs = _persons(1, 2, 3)
    _seed(conn, "rejected", refs)
    stats = reconcile_clusters(conn, 1, [_cc(*refs)])
    assert stats.memo_skipped == 1
    assert stats.new_candidates == 0


def test_memo_rechazo_difuso_suprime(conn: Connection) -> None:
    _seed(conn, "rejected", _persons(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
    cc = _cc(*_persons(1, 2, 3, 4, 5, 6, 7, 8, 9))  # Jaccard 9/10 = 0.9 ≥ 0.85 → suprimido
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.memo_skipped == 1
    assert stats.new_candidates == 0
