"""Bloqueo semántico en tareas: una tarea nueva se contrasta con las vivas del asunto que se le
parecen por SIGNIFICADO aunque no compartan palabras (van al juez como `semantic`), una
completada parecida por significado cubre, los números incompatibles vetan, y con el bloqueo
apagado todo sigue igual que antes. Los vectores se guardan al crear y al renombrar, y se
borran al fundir o borrar."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.tareas.module import (
    SEMANTIC_SLUG,
    add,
    delete,
    different_numbers,
    find_completed_cover,
    find_similar_candidates,
    merge_tareas,
    set_status,
    update,
)
from memex.semantic.embedder import FixedEmbedder, use_embedder
from memex.semantic.store import count_vectors

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

V = {
    "reclamar el pasaporte ordinario": [1.0, 0.9, 0.0, 0.0, 0.0],
    "recoger y revisar el pasaporte": [0.9, 1.0, 0.0, 0.0, 0.0],
    "solicitar el pasaporte presencialmente": [0.6, 0.6, 0.5, 0.0, 0.0],
    "renovar la cédula": [0.0, 0.0, 0.0, 1.0, 0.0],
    "leer la página 42": [0.0, 0.0, 0.0, 0.0, 1.0],
    "leer la página 43": [0.0, 0.0, 0.0, 0.0, 1.0],
    "retirar el documento de viaje": [0.95, 0.9, 0.0, 0.0, 0.0],
}


def _asunto(conn: Connection, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                "VALUES (1, :n, 'tramite', 'abierto', ARRAY[]::text[]) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def test_different_numbers_veto() -> None:
    assert different_numbers("Leer la página 42", "Leer la página 43")
    assert not different_numbers("Leer la página 42", "Leer la página 42 y 43")
    assert not different_numbers("Leer la página", "Leer la página 43")  # solo uno trae número


def test_semantic_candidates_reach_the_judge_without_shared_words(conn: Connection) -> None:
    with use_embedder(FixedEmbedder(V)):
        aid = _asunto(conn, "Sacar el pasaporte")
        recoger = add(conn, 1, title="Recoger y revisar el pasaporte", asunto=aid)
        cedula = add(conn, 1, title="Renovar la cédula", asunto=aid)
        assert count_vectors(conn, 1, SEMANTIC_SLUG) == 2
        cands = find_similar_candidates(
            conn, 1, title="Reclamar el pasaporte ordinario", asunto_id=aid, due_on=None
        )
        ids = [int(c.tarea["id"]) for c in cands]
        assert ids == [recoger["id"]]
        assert cands[0].semantic and cands[0].score >= 0.6 and not cands[0].plan_step
        assert cedula["id"] not in ids
        # una léxica va primero que una semántica
        parecida = add(conn, 1, title="Reclamar el pasaporte ordinario en la oficina", asunto=aid)
        cands = find_similar_candidates(
            conn, 1, title="Reclamar el pasaporte ordinario", asunto_id=aid, due_on=None
        )
        assert [int(c.tarea["id"]) for c in cands][:2] == [parecida["id"], recoger["id"]]
        assert not cands[0].semantic and cands[1].semantic


def test_numbers_veto_semantic_but_not_lexical(conn: Connection) -> None:
    with use_embedder(FixedEmbedder(V)):
        aid = _asunto(conn, "Curso")
        p42 = add(conn, 1, title="Leer la página 42", asunto=aid)
        cands = find_similar_candidates(
            conn, 1, title="Leer la página 43", asunto_id=aid, due_on=None
        )
        # la regla léxica (mismos tokens «leer», «página») sí la propone; la semántica no la duplica
        assert [int(c.tarea["id"]) for c in cands] == [p42["id"]]
        assert not cands[0].semantic


def test_completed_cover_by_meaning(conn: Connection) -> None:
    with use_embedder(FixedEmbedder(V)):
        aid = _asunto(conn, "Sacar el pasaporte")
        hecha = add(conn, 1, title="Retirar el documento de viaje", asunto=aid)
        set_status(
            conn, 1, hecha["id"], "completada", when=datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
        )
        cover = find_completed_cover(
            conn,
            1,
            title="Reclamar el pasaporte ordinario",
            asunto_id=aid,
            message_day=date(2026, 9, 17),
        )
        assert cover is not None and cover.tarea["id"] == hecha["id"]
        assert cover.semantic and not cover.exact and cover.score >= 0.6
        # pedida después de completarla: no cubre
        assert (
            find_completed_cover(
                conn,
                1,
                title="Reclamar el pasaporte ordinario",
                asunto_id=aid,
                message_day=date(2026, 9, 25),
            )
            is None
        )


def test_vectors_follow_the_task_lifecycle(conn: Connection) -> None:
    with use_embedder(FixedEmbedder(V)):
        aid = _asunto(conn, "Sacar el pasaporte")
        a = add(conn, 1, title="Renovar la cédula", asunto=aid)
        b = add(conn, 1, title="Recoger y revisar el pasaporte", asunto=aid)
        assert count_vectors(conn, 1, SEMANTIC_SLUG) == 2
        update(conn, 1, a["id"], title="Reclamar el pasaporte ordinario")
        cands = find_similar_candidates(
            conn, 1, title="Retirar el documento de viaje", asunto_id=aid, due_on=None
        )
        assert {int(c.tarea["id"]) for c in cands} == {a["id"], b["id"]}  # ambas por significado
        merge_tareas(conn, 1, into=b["id"], absorb=a["id"])
        assert count_vectors(conn, 1, SEMANTIC_SLUG) == 1  # la absorbida ya no compite
        delete(conn, 1, b["id"])
        assert count_vectors(conn, 1, SEMANTIC_SLUG) == 0


def test_off_keeps_the_old_behaviour(conn: Connection) -> None:
    with use_embedder(None):
        aid = _asunto(conn, "Sacar el pasaporte")
        add(conn, 1, title="Recoger y revisar el pasaporte", asunto=aid)
        assert count_vectors(conn, 1, SEMANTIC_SLUG) == 0
        cands = find_similar_candidates(
            conn, 1, title="Reclamar el pasaporte ordinario", asunto_id=aid, due_on=None
        )
        assert cands == []  # sin palabras en común y sin plan: nada, como antes
