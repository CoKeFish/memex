"""Los pasos del PLAN de un asunto siempre llegan al juez (2026-09-24): el parecido por tokens
quita el nombre del asunto («pasaporte») y no conoce sinónimos («reclamar» / «recoger»), así que
«Reclamar el pasaporte ordinario» nunca se comparaba con el paso «Recoger y revisar el pasaporte»
y nacía duplicada. Una tarea suelta se contrasta con los pasos vivos de los proyectos del asunto
(`plan_step`) y un paso ya completado cuenta como cobertura posible."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.tareas.module import (
    add,
    find_completed_cover,
    find_or_create_parent,
    find_similar_candidates,
    set_status,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _asunto(conn: Connection, name: str, kind: str = "tramite") -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                "VALUES (1, :n, :k, 'abierto', ARRAY[]::text[]) RETURNING id"
            ),
            {"n": name, "k": kind},
        ).scalar_one()
    )


def _plan(conn: Connection, aid: int) -> tuple[int, int, int]:
    proyecto = find_or_create_parent(conn, 1, title="Sacar el pasaporte", asunto_id=aid)
    pagar = add(conn, 1, title="Pagar el trámite", asunto=aid, parent=proyecto)
    recoger = add(conn, 1, title="Recoger y revisar el pasaporte", asunto=aid, parent=proyecto)
    return proyecto, int(pagar["id"]), int(recoger["id"])


def test_los_pasos_del_plan_son_candidatas_aunque_no_compartan_palabras(conn: Connection) -> None:
    aid = _asunto(conn, "Sacar el pasaporte")
    proyecto, pagar, recoger = _plan(conn, aid)
    suelta = add(conn, 1, title="Renovar la cédula", asunto=aid)  # ni una palabra en común
    cands = find_similar_candidates(
        conn, 1, title="Reclamar el pasaporte ordinario", asunto_id=aid, due_on=None
    )
    ids = {int(c.tarea["id"]) for c in cands}
    assert {pagar, recoger} <= ids  # los pasos del plan, sin exigir tokens
    assert (
        suelta["id"] not in ids and proyecto not in ids
    )  # una suelta sin parecido no; el padre no
    assert all(c.plan_step and c.score == 0.0 for c in cands if int(c.tarea["id"]) in ids)
    # una parecida por tokens (≥ 2 en común, sin contar el asunto) va ANTES que los pasos del plan
    parecida = add(conn, 1, title="Reclamar el pasaporte ordinario en la oficina", asunto=aid)
    cands = find_similar_candidates(
        conn, 1, title="Reclamar el pasaporte ordinario", asunto_id=aid, due_on=None
    )
    assert int(cands[0].tarea["id"]) == parecida["id"] and not cands[0].plan_step
    assert [c.plan_step for c in cands[1:]] == [True] * (len(cands) - 1)


def test_un_paso_no_se_contrasta_con_sus_hermanos_ni_sin_asunto(conn: Connection) -> None:
    aid = _asunto(conn, "Sacar el pasaporte")
    proyecto, _pagar, recoger = _plan(conn, aid)
    # la candidata YA es un paso del proyecto: sus hermanos no entran por ser del plan
    cands = find_similar_candidates(
        conn, 1, title="Entregar la foto", asunto_id=aid, due_on=None, parent_id=proyecto
    )
    assert recoger not in {int(c.tarea["id"]) for c in cands}
    # sin asunto no hay plan que contrastar
    assert (
        find_similar_candidates(conn, 1, title="Entregar la foto", asunto_id=None, due_on=None)
        == []
    )


def test_un_paso_completado_del_plan_es_cobertura_posible(conn: Connection) -> None:
    aid = _asunto(conn, "Sacar el pasaporte")
    _proyecto, _pagar, recoger = _plan(conn, aid)
    set_status(conn, 1, recoger, "completada", when=datetime(2026, 9, 24, 17, 0, tzinfo=UTC))
    # pedida el 17 (antes de completarla): el paso completado cubre, sin palabras en común
    cover = find_completed_cover(
        conn,
        1,
        title="Reclamar el pasaporte ordinario",
        asunto_id=aid,
        message_day=date(2026, 9, 17),
    )
    assert cover is not None and cover.tarea["id"] == recoger
    assert not cover.exact and cover.score == 0.0
    # pedida DESPUÉS de completarla: es otra asignación
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
    # una completada suelta parecida por tokens (≥ 2 en común) gana sobre el paso del plan
    suelta = add(conn, 1, title="Reclamar el pasaporte ordinario en la oficina", asunto=aid)
    set_status(conn, 1, suelta["id"], "completada", when=datetime(2026, 9, 24, 18, 0, tzinfo=UTC))
    cover = find_completed_cover(
        conn,
        1,
        title="Reclamar el pasaporte ordinario",
        asunto_id=aid,
        message_day=date(2026, 9, 17),
    )
    assert cover is not None and cover.tarea["id"] == suelta["id"] and cover.score > 0
