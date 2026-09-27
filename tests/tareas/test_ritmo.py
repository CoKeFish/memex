"""Ritmo de un asunto (`tareas/ritmo.py`): plazo típico de una consigna (mediana de los plazos
dichos), cadencia de un proyecto (en cuántos días llegaron sus pasos) y la gracia con la que vence,
todo deducido de los datos del asunto. Sin historia, las constantes de antes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import text

from memex.modules.tareas.module import add, expire_overdue, find_or_create_parent, get
from memex.modules.tareas.ritmo import (
    GRACIA_CONSIGNA,
    GRACIA_PROYECTO,
    PLAZO_DEFAULT_DIAS,
    cadencia_proyecto,
    gracia_proyecto,
    plazo_tipico,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _asunto(conn: Connection, name: str, kind: str = "programa") -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                "VALUES (1, :n, :k, 'abierto', ARRAY[]::text[]) RETURNING id"
            ),
            {"n": name, "k": kind},
        ).scalar_one()
    )


def _source(conn: Connection) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'telegram') RETURNING id"
            ),
            {"n": f"tg-{uuid4().hex[:6]}"},
        ).scalar_one()
    )


def _msg(conn: Connection, sid: int, day: date) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, :o, CAST(:p AS JSONB)) RETURNING id"
            ),
            {
                "s": sid,
                "e": uuid4().hex,
                # 15:00 Bogotá = 20:00Z: el día local es `day`
                "o": datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
                "p": json.dumps({"chat_id": 1, "text": "consigna"}),
            },
        ).scalar_one()
    )


def _tarea(
    conn: Connection,
    aid: int,
    title: str,
    *,
    msg: int,
    due: date | None = None,
    parent: int | None = None,
    metadata: dict[str, object] | None = None,
) -> int:
    row = add(
        conn,
        1,
        title=title,
        asunto=aid,
        due_on=due,
        parent=parent,
        metadata=metadata,
        source_inbox_ids=[msg],
        evidence="e",
        origin="extraction",
        force=True,
    )
    return int(row["id"])


def test_plazo_tipico_es_la_mediana_de_los_plazos_dichos(conn: Connection) -> None:
    aid = _asunto(conn, "Programa con dinámicas")
    sid = _source(conn)
    # martes → viernes (3 días), dos veces desde el MISMO mensaje: cuenta una vez
    m1 = _msg(conn, sid, date(2026, 5, 19))
    _tarea(conn, aid, "Publicar la experiencia", msg=m1, due=date(2026, 5, 22))
    _tarea(conn, aid, "Compartir el enlace", msg=m1, due=date(2026, 5, 22))
    # otra dinámica de 3 días y una estirada a una semana
    _tarea(conn, aid, "Murdoku", msg=_msg(conn, sid, date(2026, 5, 12)), due=date(2026, 5, 15))
    _tarea(conn, aid, "La larga", msg=_msg(conn, sid, date(2026, 6, 22)), due=date(2026, 6, 29))
    # una fecha INFERIDA (una_semana) no cuenta; un plazo absurdo tampoco
    _tarea(
        conn,
        aid,
        "Inferida",
        msg=_msg(conn, sid, date(2026, 6, 1)),
        due=date(2026, 6, 8),
        metadata={"due_kind": "una_semana", "due_source": "una_semana"},
    )
    _tarea(conn, aid, "Absurda", msg=_msg(conn, sid, date(2026, 6, 1)), due=date(2027, 6, 1))
    r = plazo_tipico(conn, 1, aid)
    assert (r.plazo_dias, r.muestras) == (3, 3)
    assert r.plazo_o_default == 3


def test_plazo_tipico_sin_historia_usa_el_default(conn: Connection) -> None:
    aid = _asunto(conn, "Programa nuevo")
    sid = _source(conn)
    _tarea(conn, aid, "Única", msg=_msg(conn, sid, date(2026, 5, 19)), due=date(2026, 5, 22))
    r = plazo_tipico(conn, 1, aid)
    assert r.plazo_dias is None and r.muestras == 1
    assert r.plazo_o_default == PLAZO_DEFAULT_DIAS


def test_cadencia_y_gracia_de_un_proyecto(conn: Connection) -> None:
    aid = _asunto(conn, "Curso con proyecto")
    sid = _source(conn)
    # una dinámica: todos los pasos de UN solo día → gracia = plazo típico del asunto (acotado)
    _tarea(
        conn, aid, "Dinámica vieja", msg=_msg(conn, sid, date(2026, 5, 12)), due=date(2026, 5, 15)
    )
    _tarea(
        conn, aid, "Otra dinámica", msg=_msg(conn, sid, date(2026, 5, 19)), due=date(2026, 5, 22)
    )
    dinamica = find_or_create_parent(conn, 1, title="Dinámica de la semana", asunto_id=aid)
    m = _msg(conn, sid, date(2026, 5, 26))
    _tarea(conn, aid, "Paso 1", msg=m, due=date(2026, 5, 29), parent=dinamica)
    _tarea(conn, aid, "Paso 2", msg=m, due=date(2026, 5, 29), parent=dinamica)
    assert cadencia_proyecto(conn, 1, dinamica).dias_con_pasos == 1
    assert gracia_proyecto(conn, 1, dinamica, aid) == 3
    # un proyecto que crece: pasos el 15, el 17 y el 24 → cadencia mediana 4 → gracia 8
    curso = find_or_create_parent(conn, 1, title="Proyecto de Instagram", asunto_id=aid)
    for d in (date(2026, 9, 15), date(2026, 9, 17), date(2026, 9, 24)):
        _tarea(conn, aid, f"Paso del {d.day}", msg=_msg(conn, sid, d), due=d, parent=curso)
    cad = cadencia_proyecto(conn, 1, curso)
    assert (cad.dias_con_pasos, cad.dias) == (3, 4)
    assert gracia_proyecto(conn, 1, curso, aid) == 8
    # cadencia larga: la gracia no pasa del máximo
    lento = find_or_create_parent(conn, 1, title="Proyecto lento", asunto_id=aid)
    for d in (date(2026, 3, 1), date(2026, 4, 1)):
        _tarea(conn, aid, f"Lento {d.month}", msg=_msg(conn, sid, d), due=d, parent=lento)
    assert gracia_proyecto(conn, 1, lento, aid) == GRACIA_PROYECTO[1]


def test_gracia_sin_historia_no_adivina_corto(conn: Connection) -> None:
    # Sin plazos dichos en el asunto, unos pasos de un solo día pueden ser el arranque de un
    # proyecto de curso: se conserva la gracia fija anterior (30 días), no se inventa una corta.
    aid = _asunto(conn, "Sin historia")
    sid = _source(conn)
    solo = find_or_create_parent(conn, 1, title="Consigna suelta", asunto_id=aid)
    _tarea(
        conn, aid, "Paso", msg=_msg(conn, sid, date(2026, 9, 1)), due=date(2026, 9, 3), parent=solo
    )
    assert gracia_proyecto(conn, 1, solo, aid) == GRACIA_PROYECTO[1]
    assert gracia_proyecto(conn, 1, solo, None) == GRACIA_PROYECTO[1]
    assert GRACIA_CONSIGNA[1] < GRACIA_PROYECTO[1]


def test_una_dinamica_vence_con_su_gracia_y_un_paso_nuevo_la_revive(conn: Connection) -> None:
    aid = _asunto(conn, "Programa de embajadores")
    sid = _source(conn)
    # historia: dos consignas de 3 días
    _tarea(conn, aid, "Vieja 1", msg=_msg(conn, sid, date(2026, 4, 7)), due=date(2026, 4, 10))
    _tarea(conn, aid, "Vieja 2", msg=_msg(conn, sid, date(2026, 4, 14)), due=date(2026, 4, 17))
    dinamica = find_or_create_parent(conn, 1, title="Dinámica de la semana", asunto_id=aid)
    m = _msg(conn, sid, date(2026, 5, 19))
    p1 = _tarea(conn, aid, "Publicar", msg=m, due=date(2026, 5, 22), parent=dinamica)
    p2 = _tarea(conn, aid, "Compartir", msg=m, due=date(2026, 5, 22), parent=dinamica)
    # entrega 22-may, gracia 3: el 25 todavía no (solo vencen las consignas viejas sueltas);
    # el 26 sí
    antes = set(expire_overdue(conn, 1, today=date(2026, 5, 25)))
    assert antes.isdisjoint({dinamica, p1, p2})
    ids = expire_overdue(conn, 1, today=date(2026, 5, 26))
    assert set(ids) == {dinamica, p1, p2}
    assert get(conn, 1, dinamica)["status"] == "vencida"
    # un paso nuevo con fecha vigente revive el proyecto; uno viejo (re-lectura) no
    _tarea(conn, aid, "Paso viejo", msg=m, due=date(2026, 5, 22), parent=dinamica)
    assert get(conn, 1, dinamica)["status"] == "vencida"
    _tarea(conn, aid, "Paso nuevo", msg=_msg(conn, sid, date.today()), parent=dinamica)
    assert get(conn, 1, dinamica)["status"] == "pendiente"
