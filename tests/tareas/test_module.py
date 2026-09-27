"""`tareas.add` con dedup (clave natural + difusa), orden de `list`, estados, update, asuntos."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.modules.tareas.module import (
    AsuntoNotFoundError,
    TareaNotFoundError,
    add,
    complete_from_evidence,
    delete,
    discard,
    expire_overdue,
    find_completed_cover,
    find_duplicate,
    find_or_create_parent,
    get,
    list_tareas,
    resolve_asunto,
    set_status,
    token_containment,
    update,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def _asunto(conn: Connection, name: str, *, aliases: list[str] | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                "VALUES (1, :n, 'programa', 'abierto', :a) RETURNING id"
            ),
            {"n": name, "a": aliases or []},
        ).scalar_one()
    )


def test_add_returns_public_row(conn: Connection) -> None:
    t = add(conn, 1, title="  Cambiar   punto de atención EPS ", priority="alta")
    assert t["title"] == "Cambiar punto de atención EPS"  # whitespace colapsado
    assert t["due_on"] is None and t["priority"] == "alta" and t["status"] == "pendiente"
    assert t["deduped"] is False and t["overdue"] is False and t["asunto_id"] is None


def test_add_dedups_exact_by_normalized_title(conn: Connection) -> None:
    a = add(conn, 1, title="Ejercicios página 42 del libro")
    b = add(conn, 1, title="ejercicios PAGINA 42 del libro")  # sin tilde, otra caja
    assert b["deduped"] is True and b["matched_by"] == "exact" and b["id"] == a["id"]
    n = conn.execute(text("SELECT count(*) FROM mod_tareas")).scalar()
    assert n == 1


def test_add_dedups_fuzzy_but_respects_due_and_force(conn: Connection) -> None:
    a = add(conn, 1, title="Pagar matrícula del Britano", due_on=date(2026, 9, 15))
    b = add(conn, 1, title="Pagar la matrícula del Britano")  # casi igual, sin fecha → la misma
    assert b["deduped"] is True and b["matched_by"] == "fuzzy" and b["id"] == a["id"]
    c = add(conn, 1, title="Pagar la matrícula del Britano", due_on=date(2026, 10, 15))
    assert c["deduped"] is False  # otra fecha límite = otra tarea
    d = add(conn, 1, title="Pagar matrícula del Britano", due_on=date(2026, 9, 15), force=True)
    assert d["deduped"] is False and d["id"] not in (a["id"], c["id"])


def test_dedup_scope_is_the_asunto_and_ignores_done(conn: Connection) -> None:
    britano = _asunto(conn, "Programa Intensivo Britano")
    ingles = _asunto(conn, "Curso de inglés online")
    a = add(conn, 1, title="Leer capítulo 3", asunto=britano)
    b = add(conn, 1, title="Leer capítulo 3", asunto=ingles)
    assert b["deduped"] is False and b["asunto_id"] == ingles  # mismo título, otro asunto
    suelta = add(
        conn, 1, title="Leer capítulo 3"
    )  # sin asunto: compara contra TODAS las pendientes
    assert suelta["deduped"] is True and suelta["id"] == a["id"]
    set_status(conn, 1, a["id"], "completada")
    set_status(conn, 1, b["id"], "completada")
    again = add(conn, 1, title="Leer capítulo 3", asunto=britano)
    assert again["deduped"] is False  # una tarea cumplida no bloquea repetirla


def test_find_duplicate_direct(conn: Connection) -> None:
    add(conn, 1, title="Renovar pasaporte")
    dup = find_duplicate(conn, 1, title="renovar PASAPORTE", asunto_id=None, due_on=None)
    assert dup is not None and dup.matched_by == "exact"
    assert find_duplicate(conn, 1, title="Comprar leche", asunto_id=None, due_on=None) is None


def test_list_order_overdue_then_due_then_undated(conn: Connection) -> None:
    today = date.today()
    add(conn, 1, title="Sin fecha alta", priority="alta")
    add(conn, 1, title="Sin fecha normal")
    add(conn, 1, title="Vencida", due_on=today - timedelta(days=3))
    add(conn, 1, title="Próxima", due_on=today + timedelta(days=2))
    add(conn, 1, title="Lejana", due_on=today + timedelta(days=40))
    titles = [t["title"] for t in list_tareas(conn, 1)]
    assert titles == ["Vencida", "Próxima", "Lejana", "Sin fecha alta", "Sin fecha normal"]
    over = list_tareas(conn, 1, overdue_only=True)
    assert [t["title"] for t in over] == ["Vencida"] and over[0]["overdue"] is True
    soon = list_tareas(conn, 1, due_before=today + timedelta(days=7))
    assert [t["title"] for t in soon] == ["Vencida", "Próxima"]
    assert [t["title"] for t in list_tareas(conn, 1, q="lejana")] == ["Lejana"]


def test_status_transitions_and_done_at(conn: Connection) -> None:
    t = add(conn, 1, title="Llamar a la EPS")
    done = set_status(conn, 1, t["id"], "completada")
    assert done["status"] == "completada" and done["done_at"] is not None
    assert list_tareas(conn, 1) == []  # pendientes por default
    assert [x["id"] for x in list_tareas(conn, 1, status="completada")] == [t["id"]]
    back = set_status(conn, 1, t["id"], "pendiente")
    assert back["status"] == "pendiente" and back["done_at"] is None
    canc = set_status(conn, 1, t["id"], "cancelada")
    assert canc["status"] == "cancelada"
    with pytest.raises(ValueError):
        set_status(conn, 1, t["id"], "perdida")


def test_update_fields_clear_due_and_asunto(conn: Connection) -> None:
    britano = _asunto(conn, "Programa Intensivo Britano", aliases=["Britano"])
    t = add(conn, 1, title="Taller escritura", due_on=date(2026, 9, 20), asunto="britano")
    assert t["asunto_id"] == britano and t["asunto_name"] == "Programa Intensivo Britano"
    u = update(conn, 1, t["id"], title="Taller de escritura", priority="alta", due_on=None)
    assert u["title"] == "Taller de escritura" and u["priority"] == "alta" and u["due_on"] is None
    u2 = update(conn, 1, t["id"], asunto=None)
    assert u2["asunto_id"] is None
    with pytest.raises(ValueError):
        update(conn, 1, t["id"])  # nada que actualizar
    with pytest.raises(TareaNotFoundError):
        get(conn, 1, 999_999)


def test_resolve_asunto_by_id_name_alias_and_missing(conn: Connection) -> None:
    aid = _asunto(conn, "Programa Intensivo B1", aliases=["Britano B1"])
    assert resolve_asunto(conn, 1, aid) == aid
    assert resolve_asunto(conn, 1, str(aid)) == aid
    assert resolve_asunto(conn, 1, "britano b1") == aid  # alias, normalizado
    assert resolve_asunto(conn, 1, "Intensivo") == aid  # contiene
    assert resolve_asunto(conn, 1, None) is None and resolve_asunto(conn, 1, "") is None
    with pytest.raises(AsuntoNotFoundError):
        resolve_asunto(conn, 1, "no existe tal cosa")
    with pytest.raises(AsuntoNotFoundError):
        resolve_asunto(conn, 1, 424242)


def test_delete_removes_row(conn: Connection) -> None:
    t = add(conn, 1, title="Borrar esto")
    assert delete(conn, 1, t["id"]) is True
    with pytest.raises(TareaNotFoundError):
        get(conn, 1, t["id"])


def test_dedup_by_tokens_abbreviation_and_asunto_name(conn: Connection) -> None:
    """Caso real del asistente: «Hacer los ejercicios de la página 42» y «Hacer los ejercicios de la
    pág. 42 del Britano» (mismo asunto, misma fecha) son la MISMA tarea aunque difflib quede bajo:
    tokens con prefijo («pag» ~ «pagina») y el nombre del asunto no cuenta."""
    britano = _asunto(conn, "Programa Intensivo Britano", aliases=["Britano"])
    a = add(
        conn,
        1,
        title="Hacer los ejercicios de la página 42",
        due_on=date(2026, 9, 10),
        asunto=britano,
    )
    b = add(
        conn,
        1,
        title="Hacer los ejercicios de la pág. 42 del Britano",
        due_on=date(2026, 9, 10),
        asunto=britano,
    )
    assert b["deduped"] is True and b["matched_by"] == "tokens" and b["id"] == a["id"]
    c = add(conn, 1, title="Ejercicios de la página 43", due_on=date(2026, 9, 10), asunto=britano)
    assert c["deduped"] is False  # otra página: 2 de 3 tokens no alcanza
    assert token_containment(frozenset({"a", "bb"}), frozenset()) == 0.0


def _asunto_kind(conn: Connection, name: str, kind: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                "VALUES (1, :n, :k, 'abierto', ARRAY[]::text[]) RETURNING id"
            ),
            {"n": name, "k": kind},
        ).scalar_one()
    )


def test_expire_overdue_closes_course_and_next_session_tasks_only(conn: Connection) -> None:
    hoy = date(2026, 9, 13)
    curso = _asunto_kind(conn, "Intensivo de inglés (vencidas)", "programa")
    tramite = _asunto_kind(conn, "Renovar pasaporte (vencidas)", "tramite")
    clase_pasada = add(conn, 1, title="Ver el video", asunto=curso, due_on=date(2026, 9, 8))
    clase_futura = add(conn, 1, title="Llegar puntual", asunto=curso, due_on=date(2026, 9, 14))
    sin_fecha = add(conn, 1, title="Completar la autoevaluación", asunto=curso)
    tramite_pasado = add(conn, 1, title="Llevar fotos", asunto=tramite, due_on=date(2026, 9, 1))
    proyecto = find_or_create_parent(conn, 1, title="Proyecto del curso", asunto_id=curso)
    paso_pasado = add(
        conn, 1, title="Copiar la plantilla", asunto=curso, parent=proyecto, due_on=date(2026, 9, 3)
    )
    suelta_sesion = add(
        conn,
        1,
        title="Traer el juguete",
        due_on=date(2026, 9, 7),
        metadata={"due_kind": "proxima_sesion"},
    )
    suelta_fecha = add(conn, 1, title="Pagar la matrícula", due_on=date(2026, 9, 1))

    dry = expire_overdue(conn, 1, today=hoy, apply=False)
    assert set(dry) >= {clase_pasada["id"], suelta_sesion["id"]}
    assert get(conn, 1, clase_pasada["id"])["status"] == "pendiente"  # dry-run no toca

    ids = expire_overdue(conn, 1, today=hoy)
    assert clase_pasada["id"] in ids and suelta_sesion["id"] in ids
    assert get(conn, 1, clase_pasada["id"])["status"] == "vencida"
    assert get(conn, 1, suelta_sesion["id"])["status"] == "vencida"
    for t in (clase_futura, sin_fecha, tramite_pasado, suelta_fecha, paso_pasado):
        assert get(conn, 1, t["id"])["status"] == "pendiente"
    assert get(conn, 1, paso_pasado["id"])["overdue"] is True  # atrasado, pero abierto
    assert get(conn, 1, tramite_pasado["id"])["overdue"] is True  # vencida a la vista, abierta
    # idempotente
    assert expire_overdue(conn, 1, today=hoy) == []


def test_expire_overdue_project_expires_with_its_steps_after_grace(conn: Connection) -> None:
    # Caso real (2026-09-24): «Dinámica de la semana» con dos pasos para el 22 de mayo seguía
    # pendiente en septiembre. El proyecto no tiene fecha propia (su entrega es la mayor de sus
    # pasos) y un paso nunca vencía por su cuenta: nada lo cerraba. Sin historia del asunto la
    # gracia es la fija anterior (30 días); `test_ritmo` cubre la gracia deducida del ritmo.
    hoy = date(2026, 9, 24)
    programa = _asunto_kind(conn, "Programa de embajadores (vence)", "programa")
    tramite = _asunto_kind(conn, "Trámite con pasos (no vence)", "tramite")

    dinamica = find_or_create_parent(conn, 1, title="Dinámica de la semana", asunto_id=programa)
    paso1 = add(
        conn,
        1,
        title="Publicar la experiencia",
        asunto=programa,
        parent=dinamica,
        due_on=date(2026, 5, 22),
    )
    paso2 = add(
        conn,
        1,
        title="Compartir el enlace",
        asunto=programa,
        parent=dinamica,
        due_on=date(2026, 5, 22),
    )
    hecho = add(
        conn, 1, title="Leer las reglas", asunto=programa, parent=dinamica, due_on=date(2026, 5, 20)
    )
    conn.execute(
        text("UPDATE mod_tareas SET status = 'completada' WHERE id = :i"), {"i": hecho["id"]}
    )

    # un proyecto de curso con un paso reciente sigue vivo (el curso agrega pasos cada semana)
    curso = find_or_create_parent(conn, 1, title="Proyecto de Instagram", asunto_id=programa)
    add(
        conn,
        1,
        title="Post de la festividad",
        asunto=programa,
        parent=curso,
        due_on=date(2026, 9, 16),
    )
    # un proyecto con un paso sin fecha sigue vivo
    abierto = find_or_create_parent(conn, 1, title="Proyecto abierto", asunto_id=programa)
    add(conn, 1, title="Paso sin fecha", asunto=programa, parent=abierto)
    add(conn, 1, title="Paso viejo", asunto=programa, parent=abierto, due_on=date(2026, 5, 1))
    # un proyecto de un trámite no vence aunque sus pasos sean viejos
    tramite_p = find_or_create_parent(conn, 1, title="Papeles", asunto_id=tramite)
    add(conn, 1, title="Llevar fotos", asunto=tramite, parent=tramite_p, due_on=date(2026, 5, 1))
    # dentro de la gracia (sin historia del asunto: 30 días desde la entrega) todavía no
    reciente = find_or_create_parent(conn, 1, title="Dinámica reciente", asunto_id=programa)
    add(conn, 1, title="Paso reciente", asunto=programa, parent=reciente, due_on=date(2026, 9, 1))

    ids = expire_overdue(conn, 1, today=hoy)
    assert set(ids) == {dinamica, paso1["id"], paso2["id"]}
    for tid in (dinamica, paso1["id"], paso2["id"]):
        assert get(conn, 1, tid)["status"] == "vencida"
    assert get(conn, 1, hecho["id"])["status"] == "completada"
    for tid in (curso, abierto, tramite_p, reciente):
        assert get(conn, 1, tid)["status"] == "pendiente"
    assert expire_overdue(conn, 1, today=hoy) == []  # idempotente
    # pasada la gracia, la dinámica reciente también vence
    assert set(expire_overdue(conn, 1, today=date(2026, 10, 2))) >= {reciente}


def test_expire_overdue_project_with_own_date_drags_its_steps(conn: Connection) -> None:
    hoy = date(2026, 9, 24)
    evento = _asunto_kind(conn, "Hackatón (vence)", "hackathon")
    proyecto = find_or_create_parent(conn, 1, title="Entrega del hackatón", asunto_id=evento)
    conn.execute(
        text("UPDATE mod_tareas SET due_on = :d WHERE id = :i"),
        {"d": date(2026, 9, 1), "i": proyecto},
    )
    paso = add(conn, 1, title="Subir el video", asunto=evento, parent=proyecto)
    ids = expire_overdue(conn, 1, today=hoy)
    assert set(ids) == {proyecto, paso["id"]}
    assert get(conn, 1, paso["id"])["status"] == "vencida"


def test_expired_task_blocks_dedup_and_can_reopen(conn: Connection) -> None:
    curso = _asunto_kind(conn, "Curso (dedup vencida)", "programa")
    t = add(conn, 1, title="Jugar el juego de la unidad", asunto=curso, due_on=date(2026, 9, 7))
    expire_overdue(conn, 1, today=date(2026, 9, 13))
    assert get(conn, 1, t["id"])["status"] == "vencida"
    again = add(conn, 1, title="Jugar el juego de la unidad", asunto=curso, due_on=date(2026, 9, 7))
    assert again["deduped"] is True and again["id"] == t["id"]  # no resucita como pendiente
    assert get(conn, 1, t["id"])["status"] == "vencida"
    back = set_status(conn, 1, t["id"], "pendiente")
    assert back["status"] == "pendiente"
    assert [r["id"] for r in list_tareas(conn, 1, status="vencida") if r["id"] == t["id"]] == []


def _inbox_row(conn: Connection) -> int:
    sid = int(
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'whatsapp') RETURNING id"
            ),
            {"n": f"wa-{uuid4().hex[:8]}"},
        ).scalar_one()
    )
    return int(
        conn.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, NOW(), '{}'::jsonb) RETURNING id"
            ),
            {"s": sid, "e": f"x-{uuid4().hex[:8]}"},
        ).scalar_one()
    )


def test_same_message_never_creates_a_second_task_whatever_its_state(conn: Connection) -> None:
    """Re-leer el mismo mensaje (re-extracción) reconoce la tarea aunque esté completada."""
    curso = _asunto_kind(conn, "Curso (procedencia)", "programa")
    mid = _inbox_row(conn)
    t = add(
        conn,
        1,
        title="Ver el video de la unidad 2",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    set_status(conn, 1, t["id"], "completada")
    again = add(
        conn,
        1,
        title="Ver el video de la unidad 2 y tomar notas",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert again["deduped"] is True and again["id"] == t["id"]
    assert get(conn, 1, t["id"])["status"] == "completada"
    # otro mensaje, mismo título, la anterior completada: sí es una nueva (repetir es legítimo)
    other = _inbox_row(conn)
    nueva = add(
        conn,
        1,
        title="Ver el video de la unidad 2",
        asunto=curso,
        source_inbox_ids=[other],
        origin="extraction",
    )
    assert nueva["deduped"] is False


def test_discard_blocks_recreation_and_records_feedback(conn: Connection) -> None:
    curso = _asunto_kind(conn, "Curso (descartar)", "programa")
    mid = _inbox_row(conn)
    t = add(
        conn,
        1,
        title="Llegar puntual a clase",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    row = discard(conn, 1, t["id"], note="es una exhortación, no una entrega")
    assert row["status"] == "cancelada"
    assert get(conn, 1, t["id"])["metadata"]["discard_reason"] == "no_es_tarea"
    fb = conn.execute(
        text("SELECT kinds, note FROM inbox_feedback WHERE inbox_id = :i"), {"i": mid}
    ).one()
    assert "wrong_extraction" in list(fb[0]) and "Llegar puntual" in str(fb[1])
    # mismo mensaje → la misma (descartada), no una nueva pendiente
    again = add(
        conn,
        1,
        title="Llegar puntual a clase",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert again["deduped"] is True and again["id"] == t["id"]
    # OTRO mensaje con el mismo título: la descartada también bloquea
    other = _inbox_row(conn)
    otra = add(
        conn,
        1,
        title="Llegar puntual a clase",
        asunto=curso,
        source_inbox_ids=[other],
        origin="extraction",
    )
    assert otra["deduped"] is True and otra["id"] == t["id"]
    assert get(conn, 1, t["id"])["status"] == "cancelada"
    # una cancelada "normal" (sin discard_reason) NO bloquea repetirla
    n = add(conn, 1, title="Pagar la matrícula del ciclo", asunto=curso)
    set_status(conn, 1, n["id"], "cancelada")
    assert add(conn, 1, title="Pagar la matrícula del ciclo", asunto=curso)["deduped"] is False


def test_project_parent_and_steps(conn: Connection) -> None:
    curso = _asunto_kind(conn, "Curso (proyecto)", "programa")
    mid = _inbox_row(conn)
    parent = find_or_create_parent(
        conn, 1, title="Proyecto de Canva", asunto_id=curso, source_inbox_ids=[mid]
    )
    again = find_or_create_parent(conn, 1, title="proyecto canva", asunto_id=curso)
    assert again == parent  # se reusa por nombre (normalizado / tokens)
    assert get(conn, 1, parent)["metadata"]["is_parent"] is True
    paso = add(
        conn,
        1,
        title="Paso 3: post de opinión",
        asunto=curso,
        parent=parent,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert paso["parent_id"] == parent
    # el padre no se confunde con un paso parecido
    otro = add(
        conn, 1, title="Publicar en Canva el proyecto del juguete", asunto=curso, parent=parent
    )
    assert otro["deduped"] is False and otro["parent_id"] == parent
    # un duplicado sin padre se cuelga al repetirse con padre
    suelta = add(conn, 1, title="Entregar el ensayo final", asunto=curso)
    rep = add(conn, 1, title="Entregar el ensayo final", asunto=curso, parent=parent)
    assert rep["deduped"] is True and rep["parent_id"] == parent and rep["id"] == suelta["id"]
    # un solo nivel y nunca a sí misma
    with pytest.raises(ValueError):
        add(conn, 1, title="Sub-sub paso", asunto=curso, parent=paso["id"])
    with pytest.raises(ValueError):
        update(conn, 1, parent, parent=parent)
    # descolgar y volver a colgar
    assert update(conn, 1, paso["id"], parent=None)["parent_id"] is None
    assert update(conn, 1, paso["id"], parent=parent)["parent_id"] == parent


def test_same_message_matches_with_shared_tokens(conn: Connection) -> None:
    """Tres fotos de la misma diapositiva → títulos distintos para la misma cosa; con procedencia
    común alcanza con ≥60 % y ≥2 tokens compartidos. Una sola palabra en común no junta; y sin
    procedencia común sigue el umbral normal."""
    curso = _asunto_kind(conn, "Curso (procedencia laxa)", "programa")
    mid = _inbox_row(conn)
    t = add(
        conn,
        1,
        title="Completar la autoevaluación (self-assessment)",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    set_status(conn, 1, t["id"], "completada")
    again = add(
        conn,
        1,
        title="Completar la autoevaluación de desempeño",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert again["deduped"] is True and again["id"] == t["id"]
    otra_cosa = add(
        conn,
        1,
        title="Completar los ejercicios de la plataforma",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert otra_cosa["deduped"] is False  # solo «completar» en común: otra tarea
    other = _inbox_row(conn)
    distinta = add(
        conn,
        1,
        title="Completar la autoevaluación de desempeño",
        asunto=curso,
        source_inbox_ids=[other],
        origin="extraction",
    )
    assert distinta["deduped"] is False  # otro mensaje: el umbral normal no la junta


def test_only_not_a_task_discards_block_by_title(conn: Connection) -> None:
    """Una descartada como DUPLICADA no bloquea: su gemela sigue viva y re-extraer debe poder
    crear/reconocer la tarea real; la descartada «no es tarea» sí bloquea."""
    curso = _asunto_kind(conn, "Curso (bloqueo)", "programa")
    mid = _inbox_row(conn)
    dup = add(
        conn,
        1,
        title="Describir tu juguete favorito en un post",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    discard(conn, 1, dup["id"], reason="duplicada")
    # mismo mensaje re-extraído: la copia muerta no absorbe; nace la tarea real
    again = add(
        conn,
        1,
        title="Describir tu juguete favorito en un post",
        asunto=curso,
        source_inbox_ids=[mid],
        origin="extraction",
    )
    assert again["deduped"] is False and again["id"] != dup["id"]
    # «no es tarea» sí bloquea, con o sin procedencia
    nope = add(conn, 1, title="Participar activamente", asunto=curso)
    discard(conn, 1, nope["id"])
    assert add(conn, 1, title="Participar activamente", asunto=curso)["deduped"] is True


def test_completed_cover_rules(conn: Connection) -> None:
    """Una completada cubre candidatas parecidas pedidas hasta su fecha de completado; no cubre
    lo pedido después, ni lo de otro asunto, ni lo que comparte una sola palabra."""
    from datetime import datetime as _dt

    curso = _asunto_kind(conn, "Curso (cover)", "programa")
    otro = _asunto_kind(conn, "Otro curso (cover)", "programa")
    t = add(conn, 1, title="Completar la autoevaluación (self-assessment)", asunto=curso)
    set_status(conn, 1, t["id"], "completada", when=_dt(2026, 9, 13, 12, 0, tzinfo=UTC))
    exact = find_completed_cover(
        conn,
        1,
        title="completar la autoevaluación (self-assessment)",
        asunto_id=curso,
        message_day=date(2026, 9, 7),
    )
    assert exact is not None and exact.exact and exact.tarea["id"] == t["id"]
    similar = find_completed_cover(
        conn,
        1,
        title="Completar la autoevaluación con estrellas",
        asunto_id=curso,
        message_day=date(2026, 9, 8),
    )
    assert similar is not None and not similar.exact and similar.tarea["id"] == t["id"]
    # pedida DESPUÉS de completarla → asignación nueva
    assert (
        find_completed_cover(
            conn,
            1,
            title="Completar la autoevaluación con estrellas",
            asunto_id=curso,
            message_day=date(2026, 9, 14),
        )
        is None
    )
    # una sola palabra en común → no
    assert (
        find_completed_cover(
            conn, 1, title="Completar los ejercicios", asunto_id=curso, message_day=date(2026, 9, 8)
        )
        is None
    )
    # otro asunto / sin asunto / sin fecha → no
    assert (
        find_completed_cover(
            conn,
            1,
            title="Completar la autoevaluación",
            asunto_id=otro,
            message_day=date(2026, 9, 8),
        )
        is None
    )
    assert (
        find_completed_cover(
            conn,
            1,
            title="Completar la autoevaluación",
            asunto_id=None,
            message_day=date(2026, 9, 8),
        )
        is None
    )
    assert (
        find_completed_cover(
            conn, 1, title="Completar la autoevaluación", asunto_id=curso, message_day=None
        )
        is None
    )


def test_complete_from_evidence(conn: Connection) -> None:
    from datetime import datetime as _dt

    curso = _asunto_kind(conn, "Curso (hecha)", "programa")
    mid = _inbox_row(conn)
    when = _dt(2026, 9, 7, 21, 0, tzinfo=UTC)
    pend = add(conn, 1, title="Completar la autoevaluación con estrellas", asunto=curso)
    tid, how = complete_from_evidence(
        conn,
        1,
        title="Completar la autoevaluación",
        asunto_id=curso,
        source_inbox_ids=[mid],
        evidence="foto de la autoevaluación llena",
        when=when,
    )
    assert (tid, how) == (pend["id"], "completed")
    row = get(conn, 1, tid)
    assert row["status"] == "completada" and row["done_at"] == when
    assert row["metadata"]["done_evidence"].startswith("foto") and mid in row["source_inbox_ids"]
    # repetir: ya estaba completada → solo procedencia
    other = _inbox_row(conn)
    tid2, how2 = complete_from_evidence(
        conn,
        1,
        title="Completar la autoevaluación",
        asunto_id=curso,
        source_inbox_ids=[other],
        evidence="otra foto",
        when=when,
    )
    assert (tid2, how2) == (tid, "already")
    assert other in get(conn, 1, tid)["source_inbox_ids"]
    # no existía → nace completada
    tid3, how3 = complete_from_evidence(
        conn,
        1,
        title="Publicar el post del juguete",
        asunto_id=curso,
        source_inbox_ids=[mid],
        evidence="mi post",
        when=when,
    )
    assert how3 == "created" and get(conn, 1, tid3)["status"] == "completada"


def test_sibling_steps_dedup_with_relaxed_threshold(conn: Connection) -> None:
    """Dos pasos del MISMO proyecto que comparten la mayoría de las palabras son el mismo paso; en
    proyectos distintos, o sin proyecto, rige el umbral normal (y no se juntan)."""
    a_title = "Describir tu foto, juguete u objeto en un post de la plataforma"
    b_title = "Publicar un post sobre tu juguete favorito en la plataforma"
    # hermanos → mismo paso
    curso = _asunto_kind(conn, "Curso (hermanos)", "programa")
    proy = find_or_create_parent(conn, 1, title="Proyecto de diapositivas", asunto_id=curso)
    a = add(conn, 1, title=a_title, asunto=curso, parent=proy)
    b = add(conn, 1, title=b_title, asunto=curso, parent=proy)
    assert b["deduped"] is True and b["id"] == a["id"]
    # proyectos distintos → dos pasos
    curso2 = _asunto_kind(conn, "Curso (hermanos 2)", "programa")
    p1 = find_or_create_parent(conn, 1, title="Proyecto uno", asunto_id=curso2)
    p2 = find_or_create_parent(conn, 1, title="Proyecto dos", asunto_id=curso2)
    add(conn, 1, title=a_title, asunto=curso2, parent=p1)
    c = add(conn, 1, title=b_title, asunto=curso2, parent=p2)
    assert c["deduped"] is False
    # sin proyecto → umbral normal → dos tareas
    curso3 = _asunto_kind(conn, "Curso (hermanos 3)", "programa")
    p3 = find_or_create_parent(conn, 1, title="Proyecto tres", asunto_id=curso3)
    add(conn, 1, title=a_title, asunto=curso3, parent=p3)
    d = add(conn, 1, title=b_title, asunto=curso3)
    assert d["deduped"] is False


def test_find_parent_only_looks_up(conn: Connection) -> None:
    from memex.modules.tareas.module import find_parent

    curso = _asunto_kind(conn, "Curso (find_parent)", "programa")
    assert find_parent(conn, 1, title="Proyecto de video", asunto_id=curso) is None
    pid = find_or_create_parent(conn, 1, title="Proyecto de video", asunto_id=curso)
    assert find_parent(conn, 1, title="proyecto video", asunto_id=curso) == pid


def test_find_parent_generic_name_means_the_only_live_project(conn: Connection) -> None:
    from memex.modules.tareas.module import find_parent

    curso = _asunto_kind(conn, "Curso (find_parent genérico)", "programa")
    pid = find_or_create_parent(conn, 1, title="Proyecto de Instagram en Canva", asunto_id=curso)
    assert find_parent(conn, 1, title="Proyecto en curso", asunto_id=curso) == pid
    assert find_parent(conn, 1, title="el proyecto final", asunto_id=curso) == pid
    # un nombre específico que no casa no es el mismo proyecto
    assert find_parent(conn, 1, title="Proyecto de video", asunto_id=curso) is None
    # con DOS proyectos vivos el genérico ya no decide
    find_or_create_parent(conn, 1, title="Proyecto de video", asunto_id=curso)
    assert find_parent(conn, 1, title="Proyecto en curso", asunto_id=curso) is None


def test_expire_overdue_hackathon_tasks_dated_or_stale(conn: Connection) -> None:
    """Evento/hackatón: con fecha vence al pasar la fecha; SIN fecha vence cuando el mensaje de
    origen tiene más de 30 días (el evento pasó). Un curso sin fecha no; una manual tampoco."""
    hoy = date(2026, 9, 13)
    hack = _asunto_kind(conn, "Hackatón (vencidas)", "hackathon")
    curso = _asunto_kind(conn, "Curso (sin fecha, vencidas)", "programa")
    src = int(
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'telegram') RETURNING id"
            ),
            {"n": f"tg-{uuid4().hex[:6]}"},
        ).scalar_one()
    )

    def inbox(when: date) -> int:
        return int(
            conn.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, :o, '{}'::jsonb) RETURNING id"
                ),
                {"s": src, "e": uuid4().hex, "o": datetime.combine(when, time(12), tzinfo=UTC)},
            ).scalar_one()
        )

    viejo, reciente = inbox(date(2026, 6, 20)), inbox(date(2026, 9, 10))
    dated = add(conn, 1, title="Registrar el equipo", asunto=hack, due_on=date(2026, 6, 25))
    stale = add(
        conn,
        1,
        title="Mirar lo de ingeniería inversa",
        asunto=hack,
        source_inbox_ids=[viejo],
        origin="extraction",
    )
    fresh = add(
        conn,
        1,
        title="Preparar el pitch",
        asunto=hack,
        source_inbox_ids=[reciente],
        origin="extraction",
    )
    manual = add(conn, 1, title="Comprar stickers", asunto=hack)
    curso_viejo = add(
        conn,
        1,
        title="Activar la cuenta",
        asunto=curso,
        source_inbox_ids=[viejo],
        origin="extraction",
    )
    ids = expire_overdue(conn, 1, today=hoy)
    assert set(ids) == {dated["id"], stale["id"]}
    for t in (fresh, manual, curso_viejo):
        assert get(conn, 1, t["id"])["status"] == "pendiente"
