"""Reconciliación de cúmulos (modelo PARTIDOR): nuevo candidato, re-detección sin duplicar, blob ya
particionado = estable (por `blob_signature`), blob derivado = nuevo candidato (sin disolver el
hijo), candidato sin detectar borrado, confirmed sin miembros en blobs disuelto, memo estable.
Refs sintéticos + seed directo."""

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
    """Un cúmulo persistido (`blob_signature` = su propia firma: representa un blob 1-a-1)."""
    members = frozenset(Ref(s, i) for s, i in refs)
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


def test_nuevo_candidato(conn: Connection) -> None:
    cc = _cc(*_persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [cc])
    assert stats.new_candidates == 1
    [c] = load_clusters(conn, 1, ("candidate",))
    assert c.members == cc.member_set


def test_redetectar_candidato_no_duplica(conn: Connection) -> None:
    cc = _cc(*_persons(1, 2, 3))
    reconcile_clusters(conn, 1, [cc])
    stats = reconcile_clusters(conn, 1, [cc])  # mismo blob, aún sin particionar
    assert stats.new_candidates == 0 and stats.memo_skipped == 1
    n = conn.execute(text("SELECT count(*) FROM relation_clusters WHERE user_id = 1")).scalar_one()
    assert n == 1


def test_blob_particionado_es_estable(conn: Connection) -> None:
    # un hijo confirmed cuyo blob_signature = el blob detectado → re-detectar = ESTABLE (sin LLM).
    _seed(conn, "confirmed", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    assert stats.matched_same == 1 and stats.new_candidates == 0
    assert load_clusters(conn, 1, ("candidate",)) == []


def test_blob_derivado_es_nuevo_candidato(conn: Connection) -> None:
    # hijo confirmed del blob {1,2,3}; detectar el blob DERIVADO {1,2,3,4} → nuevo candidato (el
    # partidor re-particiona y sincroniza); el hijo NO se disuelve (sus miembros siguen en el blob).
    cid = _seed(conn, "confirmed", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3, 4))])
    assert stats.new_candidates == 1
    assert _status_of(conn, cid) == "confirmed"


def test_candidato_sin_detectar_borrado(conn: Connection) -> None:
    reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    stats = reconcile_clusters(conn, 1, [])
    assert stats.deleted == 1
    assert load_clusters(conn, 1, ("candidate",)) == []


def test_confirmed_sin_miembros_en_blobs_disuelto(conn: Connection) -> None:
    cid = _seed(conn, "confirmed", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [_cc(*_persons(7, 8, 9))])  # blob ajeno
    assert stats.dissolved == 1
    assert _status_of(conn, cid) == "dissolved"


def test_memo_rechazo_exacto_es_estable(conn: Connection) -> None:
    # un memo rejected con la firma del blob → re-detectarlo NO crea candidato (estable).
    _seed(conn, "rejected", _persons(1, 2, 3))
    stats = reconcile_clusters(conn, 1, [_cc(*_persons(1, 2, 3))])
    assert stats.matched_same == 1 and stats.new_candidates == 0
