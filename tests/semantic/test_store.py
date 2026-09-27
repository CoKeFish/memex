"""Bloqueo semántico (`memex.semantic`): vectores guardados por (slug, ref_id, modelo), sin
re-embeber lo que no cambió; vecinos por coseno dentro de un alcance, top-k con piso; apagado
(`use_embedder(None)`) todo devuelve vacío y nada falla."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.semantic.embedder import FixedEmbedder, cosine, normalize, use_embedder
from memex.semantic.store import (
    count_vectors,
    delete_vectors,
    neighbors,
    text_hash,
    upsert_vectors,
)

V = {
    "reclamar el pasaporte ordinario": [1.0, 0.9, 0.0, 0.0],
    "recoger y revisar el pasaporte": [0.9, 1.0, 0.0, 0.0],
    "solicitar el pasaporte presencialmente": [0.7, 0.7, 0.4, 0.0],
    "renovar la cédula": [0.0, 0.0, 0.0, 1.0],
}


def _rows(conn: Any, slug: str) -> list[tuple[int, str, int]]:
    return [
        (int(r[0]), str(r[1]), int(r[2]))
        for r in conn.execute(
            text(
                "SELECT ref_id, text_hash, dim FROM semantic_vectors "
                "WHERE user_id = 1 AND slug = :s ORDER BY ref_id"
            ),
            {"s": slug},
        ).all()
    ]


def test_cosine_and_normalize() -> None:
    assert cosine(normalize([1, 0]), normalize([1, 0])) == 1.0
    assert cosine(normalize([1, 0]), normalize([0, 1])) == 0.0
    assert normalize([0, 0]) == [0.0, 0.0]


def test_upsert_skips_unchanged_and_refreshes_changed(conn: Any) -> None:
    emb = FixedEmbedder(V)
    with use_embedder(emb):
        n = upsert_vectors(
            conn, 1, "t", [(1, "Reclamar el pasaporte ordinario"), (2, "Renovar la cédula")]
        )
        assert n == 2 and emb.calls == 1
        assert [(r[0], r[2]) for r in _rows(conn, "t")] == [(1, 4), (2, 4)]
        # mismo texto (otra caja/espacios): no se re-embebe
        assert upsert_vectors(conn, 1, "t", [(1, "  reclamar  el pasaporte ORDINARIO ")]) == 0
        assert emb.calls == 1
        # texto nuevo para el mismo id: se refresca (mismo ref_id, otro hash)
        h_before = _rows(conn, "t")[0][1]
        assert upsert_vectors(conn, 1, "t", [(1, "Recoger y revisar el pasaporte")]) == 1
        assert _rows(conn, "t")[0][1] != h_before
        assert text_hash("Hola  Mundo") == text_hash("hola mundo")
        assert count_vectors(conn, 1, "t") == 2
        assert delete_vectors(conn, 1, "t", [2]) == 1
        assert count_vectors(conn, 1, "t") == 1


def test_neighbors_top_k_with_floor_within_scope(conn: Any) -> None:
    emb = FixedEmbedder(V)
    with use_embedder(emb):
        upsert_vectors(
            conn,
            1,
            "t",
            [
                (10, "Recoger y revisar el pasaporte"),
                (11, "Solicitar el pasaporte presencialmente"),
                (12, "Renovar la cédula"),
                (13, "Fuera del alcance"),
            ],
        )
        found = neighbors(
            conn,
            1,
            "t",
            "Reclamar el pasaporte ordinario",
            scope_ids=[10, 11, 12],
            k=3,
            min_score=0.6,
        )
        assert [n.ref_id for n in found] == [10, 11]  # 12 (cédula) queda bajo el piso
        assert found[0].score > found[1].score >= 0.6
        # top-k recorta; el alcance manda (13 no entra aunque exista)
        assert [
            n.ref_id
            for n in neighbors(
                conn,
                1,
                "t",
                "Reclamar el pasaporte ordinario",
                scope_ids=[10, 11, 12, 13],
                k=1,
                min_score=0.0,
            )
        ] == [10]
        # un id del alcance sin vector no compite; sin alcance, nada
        assert neighbors(conn, 1, "t", "x", scope_ids=[999], k=3, min_score=0.0) == []
        assert neighbors(conn, 1, "t", "x", scope_ids=[], k=3, min_score=0.0) == []


def test_off_means_nothing_happens(conn: Any) -> None:
    with use_embedder(None):
        assert upsert_vectors(conn, 1, "t", [(1, "algo")]) == 0
        assert neighbors(conn, 1, "t", "algo", scope_ids=[1], k=3, min_score=0.0) == []
        assert count_vectors(conn, 1, "t") == 0


def test_vectors_of_another_model_do_not_compete(conn: Any) -> None:
    a = FixedEmbedder(V)
    b = FixedEmbedder(V)
    b.model = "fixed-other"
    with use_embedder(a):
        upsert_vectors(conn, 1, "t", [(1, "Recoger y revisar el pasaporte")])
    with use_embedder(b):
        assert (
            neighbors(
                conn, 1, "t", "Reclamar el pasaporte ordinario", scope_ids=[1], k=3, min_score=0.0
            )
            == []
        )
