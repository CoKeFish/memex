"""`TareasModule`: tareas extraídas de mensajes entran por `add` con procedencia; dedup; asunto por
evidencia, por pista o por adopción; re-extracción olvida solo lo extraído; el orquestador no enruta
tareas en chats de anuncios."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules import known_modules, resolve
from memex.modules.asuntos import store as asuntos_store
from memex.modules.contract import ModuleContext
from memex.modules.tareas import module as tareas
from memex.modules.tareas.extract import TareasModule
from memex.modules.tareas.schema import TareaItem


def _source(stype: str = "imap", mode: str | None = None) -> int:
    cfg: dict[str, Any] = {}
    if mode:
        cfg["allowed_chats"] = [{"chat_id": "g1@g.us", "mode": mode}]
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type, config) "
                    "VALUES (1, :n, :t, CAST(:cfg AS JSONB)) RETURNING id"
                ),
                {"n": f"{stype}-{uuid4().hex[:6]}", "t": stype, "cfg": json.dumps(cfg)},
            ).scalar_one()
        )


def _inbox(source_id: int, payload: dict[str, Any] | None = None) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": source_id, "e": uuid4().hex, "p": json.dumps(payload or {"subject": "x"})},
            ).scalar_one()
        )


def _asunto(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                    "VALUES (1, :n, 'programa', 'abierto', ARRAY[]::text[]) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _persist(inbox_ids: tuple[int, ...], items: list[TareaItem]) -> int:
    async def run() -> int:
        with connection() as conn:
            ctx = ModuleContext(
                user_id=1,
                conn=conn,
                llm=cast(LLMClient, None),
                deps={},
                summary_id=None,
                inbox_ids=inbox_ids,
            )
            return await TareasModule().persist(ctx, items)

    return asyncio.run(run())


def _rows(inbox_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT id, title, due_on, priority, status, asunto_id, evidence, origin, "
                    "source_inbox_ids, parent_id FROM mod_tareas WHERE user_id = 1 "
                    "AND :i = ANY(source_inbox_ids) ORDER BY id"
                ),
                {"i": inbox_id},
            )
            .mappings()
            .all()
        ]


def test_module_is_registered_as_extractor() -> None:
    assert "tareas" in known_modules()
    mod = resolve("tareas")()
    assert mod.slug == "tareas" and "extract" in mod.capabilities
    assert mod.extraction_schema is TareaItem


def test_schema_normalizes_dates_and_priority() -> None:
    it = TareaItem(
        source_inbox_ids=(1,),
        title="  Entregar   ensayo ",
        due_on="2026-09-18",
        priority="urgente",
        evidence="entregar ensayo el jueves",
    )
    assert it.title == "Entregar ensayo" and it.due_on == date(2026, 9, 18)
    assert it.priority == "alta"
    assert TareaItem(source_inbox_ids=(1,), title="x", due_on="pronto").due_on is None
    assert TareaItem(source_inbox_ids=(1,), title="x", due_on="").due_on is None
    with pytest.raises(ValueError):
        TareaItem(source_inbox_ids=(1,), title="   ")
    with pytest.raises(ValueError):
        TareaItem(source_inbox_ids=(1,), title="x", inventado="y")  # type: ignore[call-arg]


def test_extracted_task_lands_with_provenance_and_dedups() -> None:
    src = _source()
    mid = _inbox(src)
    name = f"Entregar ensayo {uuid4().hex[:5]}"
    items = [
        TareaItem(
            source_inbox_ids=(mid,),
            title=name,
            # en el futuro: un plazo ya vencido cuando llega la segunda cita ya no funde solo
            # (regla temporal, test_dedup_temporal.py)
            due_on=date(2099, 9, 18),
            priority="alta",
            evidence="entregar el ensayo el jueves 18",
        )
    ]
    assert _persist((mid,), items) == 1
    (row,) = _rows(mid)
    assert row["title"] == name and row["due_on"] == date(2099, 9, 18)
    assert row["priority"] == 1 and row["origin"] == "extraction"
    assert row["evidence"] == "entregar el ensayo el jueves 18"
    assert list(row["source_inbox_ids"]) == [mid]
    # el mismo pendiente citado por otro mensaje: no se duplica, suma procedencia
    mid2 = _inbox(src)
    assert _persist((mid2,), [TareaItem(source_inbox_ids=(mid2,), title=name)]) == 1
    assert _rows(mid2) == _rows(mid)
    assert sorted(_rows(mid)[0]["source_inbox_ids"]) == sorted([mid, mid2])


def test_asunto_by_existing_evidence_and_by_hint() -> None:
    src = _source()
    curso = _asunto(f"Intensivo de inglés {uuid4().hex[:5]}")
    m_ev = _inbox(src)
    with connection() as c:
        asuntos_store.add_evidence(c, 1, curso, m_ev, method="manual", confidence=1.0)
    _persist((m_ev,), [TareaItem(source_inbox_ids=(m_ev,), title=f"Tarea A {uuid4().hex[:4]}")])
    assert _rows(m_ev)[0]["asunto_id"] == curso

    tramite = _asunto(f"Renovar pasaporte {uuid4().hex[:5]}")
    m_hint = _inbox(src)
    with connection() as c:
        nombre = c.execute(
            text("SELECT name FROM mod_asuntos WHERE id = :a"), {"a": tramite}
        ).scalar_one()
    _persist(
        (m_hint,),
        [TareaItem(source_inbox_ids=(m_hint,), title="Llevar fotos", asunto_hint=str(nombre))],
    )
    assert _rows(m_hint)[0]["asunto_id"] == tramite

    m_none = _inbox(src)
    _persist(
        (m_none,),
        [TareaItem(source_inbox_ids=(m_none,), title="Algo suelto", asunto_hint="no existe tal")],
    )
    assert _rows(m_none)[0]["asunto_id"] is None


def test_asunto_adopts_orphan_task_when_evidence_arrives() -> None:
    src = _source()
    mid = _inbox(src)
    _persist((mid,), [TareaItem(source_inbox_ids=(mid,), title=f"Suelta {uuid4().hex[:4]}")])
    assert _rows(mid)[0]["asunto_id"] is None
    asunto = _asunto(f"Cohorte {uuid4().hex[:5]}")
    with connection() as c:
        asuntos_store.add_evidence(c, 1, asunto, mid, method="manual", confidence=0.9)
    assert _rows(mid)[0]["asunto_id"] == asunto
    # una tarea MANUAL sin procedencia no se toca
    with connection() as c:
        manual = tareas.add(c, 1, title=f"Manual {uuid4().hex[:4]}")
        assert manual["asunto_id"] is None


def test_forget_inbox_removes_only_extracted_orphans() -> None:
    src = _source()
    mid = _inbox(src)
    _persist((mid,), [TareaItem(source_inbox_ids=(mid,), title=f"Extraída {uuid4().hex[:4]}")])
    with connection() as c:
        manual = tareas.add(c, 1, title=f"Manual {uuid4().hex[:4]}")
        removed = TareasModule().forget_inbox(c, 1, [mid])
    assert removed == 1
    assert _rows(mid) == []
    with connection() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM mod_tareas WHERE id = :i"), {"i": manual["id"]}
            ).scalar_one()
            == 1
        )


def test_read_for_inbox_lists_extracted_tasks() -> None:
    src = _source()
    mid = _inbox(src)
    _persist((mid,), [TareaItem(source_inbox_ids=(mid,), title="Leer capítulo 2")])
    with connection() as c:
        out = TareasModule().read_for_inbox(c, 1, [mid])
    assert [o["title"] for o in out] == ["Leer capítulo 2"]


# --- asunto por el chat y fecha «próxima sesión» --------------------------------------------- #


def _chat_inbox(source_id: int, chat_id: str, when: str = "2026-09-07 20:00:00+00") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, CAST(:o AS TIMESTAMPTZ), CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "s": source_id,
                    "e": uuid4().hex,
                    "o": when,
                    "p": json.dumps({"chat_id": chat_id, "chat_title": "Curso", "text": "foto"}),
                },
            ).scalar_one()
        )


def _recurring_class(inbox_id: int, title: str, *, commitment: str = "confirmado") -> int:
    """Clase de lunes a viernes 15:10 desde el 2026-09-02, ligada al mensaje `inbox_id`."""
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on, "
                    "start_time, origin, recurrence, commitment) "
                    "VALUES (1, CAST(:ids AS BIGINT[]), :t, DATE '2026-09-02', TIME '15:10', "
                    "'extraction', ARRAY['RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925'], "
                    ":c) "
                    "RETURNING id"
                ),
                {"ids": [inbox_id], "t": title, "c": commitment},
            ).scalar_one()
        )


def test_task_from_a_course_chat_hangs_from_the_chat_dominant_asunto() -> None:
    """El asignador no decidió la foto del tablero, pero el chat es del curso: la tarea va al curso.
    Con pocas evidencias o un chat que mezcla asuntos, no se asume nada."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    decided = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in decided:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    foto = _chat_inbox(src, chat)
    _persist(
        (foto,), [TareaItem(source_inbox_ids=(foto,), title=f"Ver el video {uuid4().hex[:4]}")]
    )
    assert _rows(foto)[0]["asunto_id"] == curso

    # otro chat con UNA sola evidencia: insuficiente → suelta
    chat2 = f"{uuid4().hex[:12]}@g.us"
    one = _chat_inbox(src, chat2)
    with connection() as c:
        asuntos_store.add_evidence(c, 1, curso, one, method="manual", confidence=1.0)
    foto2 = _chat_inbox(src, chat2)
    _persist((foto2,), [TareaItem(source_inbox_ids=(foto2,), title=f"Leer {uuid4().hex[:4]}")])
    assert _rows(foto2)[0]["asunto_id"] is None

    # chat que mezcla dos asuntos a partes iguales → suelta
    chat3 = f"{uuid4().hex[:12]}@g.us"
    otro = _asunto(f"Otro {uuid4().hex[:5]}")
    with connection() as c:
        for aid in (curso, curso, curso, otro, otro, otro):
            asuntos_store.add_evidence(
                c, 1, aid, _chat_inbox(src, chat3), method="manual", confidence=1.0
            )
    foto3 = _chat_inbox(src, chat3)
    _persist((foto3,), [TareaItem(source_inbox_ids=(foto3,), title=f"Mixto {uuid4().hex[:4]}")])
    assert _rows(foto3)[0]["asunto_id"] is None


def test_due_next_session_comes_from_the_course_calendar() -> None:
    """«Class preparation» un lunes por la noche → vence el martes (siguiente clase del curso). Sin
    calendario del asunto, la tarea queda sin fecha. Una tarea repetida sin fecha toma la fecha."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    _recurring_class(seed[0], f"Programa Intensivo {uuid4().hex[:4]}")

    lunes = _chat_inbox(
        src, chat, when="2026-09-08 01:00:00+00"
    )  # lunes 7 de sept a las 20:00 Bogotá
    title = f"Traer el juguete {uuid4().hex[:4]}"
    _persist(
        (lunes,), [TareaItem(source_inbox_ids=(lunes,), title=title, due_kind="proxima_sesion")]
    )
    row = _rows(lunes)[0]
    assert (row["asunto_id"], row["due_on"]) == (curso, date(2026, 9, 8))

    # viernes → la próxima clase es el lunes
    viernes = _chat_inbox(src, chat, when="2026-09-11 23:00:00+00")
    _persist(
        (viernes,),
        [
            TareaItem(
                source_inbox_ids=(viernes,),
                title=f"Leer {uuid4().hex[:4]}",
                due_kind="proxima_sesion",
            )
        ],
    )
    assert _rows(viernes)[0]["due_on"] == date(2026, 9, 14)

    # la misma tarea ya existía sin fecha (primera lectura sin due_kind) → la toma al repetirse
    sin_fecha = _chat_inbox(src, chat, when="2026-09-09 01:00:00+00")
    t2 = f"Repasar la unidad {uuid4().hex[:4]}"
    _persist((sin_fecha,), [TareaItem(source_inbox_ids=(sin_fecha,), title=t2)])
    assert _rows(sin_fecha)[0]["due_on"] is None
    _persist(
        (sin_fecha,),
        [TareaItem(source_inbox_ids=(sin_fecha,), title=t2, due_kind="proxima_sesion")],
    )
    assert _rows(sin_fecha)[0]["due_on"] == date(2026, 9, 9)

    # curso SIN calendario → sin fecha
    src2 = _source("whatsapp", mode="programa")
    chat2 = f"{uuid4().hex[:12]}@g.us"
    curso2 = _asunto(f"Taller {uuid4().hex[:5]}")
    with connection() as c:
        for _ in range(3):
            asuntos_store.add_evidence(
                c, 1, curso2, _chat_inbox(src2, chat2), method="manual", confidence=1.0
            )
    m = _chat_inbox(src2, chat2)
    _persist(
        (m,),
        [
            TareaItem(
                source_inbox_ids=(m,), title=f"Algo {uuid4().hex[:4]}", due_kind="proxima_sesion"
            )
        ],
    )
    assert (_rows(m)[0]["asunto_id"], _rows(m)[0]["due_on"]) == (curso2, None)


def test_relink_orphans_applies_current_rule() -> None:
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    foto = _chat_inbox(src, chat, when="2026-09-08 01:00:00+00")
    # se extrajo ANTES de que el chat tuviera evidencia: quedó suelta, marcada «próxima sesión»
    _persist(
        (foto,),
        [
            TareaItem(
                source_inbox_ids=(foto,),
                title=f"Ver video {uuid4().hex[:4]}",
                due_kind="proxima_sesion",
            )
        ],
    )
    row = _rows(foto)[0]
    assert (row["asunto_id"], row["due_on"]) == (None, None)
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    _recurring_class(seed[0], f"Programa Intensivo {uuid4().hex[:4]}")

    with connection() as c:
        dry = tareas.relink_orphans(c, 1, apply=False)
    assert dry["linked"] >= 1 and dry["dated"] >= 1
    assert _rows(foto)[0]["asunto_id"] is None  # dry-run no toca
    with connection() as c:
        tareas.relink_orphans(c, 1, apply=True)
    row = _rows(foto)[0]
    assert (row["asunto_id"], row["due_on"]) == (curso, date(2026, 9, 8))


def test_schema_due_kind_normalizes() -> None:
    assert (
        TareaItem(source_inbox_ids=(1,), title="x", due_kind="próxima clase").due_kind
        == "proxima_sesion"
    )
    assert TareaItem(source_inbox_ids=(1,), title="x", due_kind="").due_kind == ""
    assert TareaItem(source_inbox_ids=(1,), title="x", due_kind=None).due_kind == ""


def test_siblings_of_a_next_session_item_inherit_the_due_kind() -> None:
    """Una lista «class preparation» con 3 ítems: el LLM marcó solo uno como próxima sesión; los
    otros dos, sin fecha y del mismo mensaje, heredan el marco. Uno con fecha explícita la
    conserva."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    _recurring_class(seed[0], f"Programa Intensivo {uuid4().hex[:4]}")
    foto = _chat_inbox(src, chat, when="2026-09-08 01:00:00+00")  # lunes 7 sept, 20:00 Bogotá
    otro = _chat_inbox(src, chat, when="2026-09-08 01:00:00+00")
    tag = uuid4().hex[:4]
    _persist(
        (foto, otro),
        [
            TareaItem(
                source_inbox_ids=(foto,), title=f"Ver video {tag}", due_kind="proxima_sesion"
            ),
            TareaItem(source_inbox_ids=(foto,), title=f"Jugar el juego {tag}"),
            TareaItem(
                source_inbox_ids=(foto,), title=f"Entregar ensayo {tag}", due_on=date(2026, 9, 18)
            ),
            TareaItem(
                source_inbox_ids=(otro,), title=f"Otra cosa {tag}"
            ),  # otro mensaje: no hereda
        ],
    )
    by_title = {r["title"]: r for r in _rows(foto) + _rows(otro)}
    assert by_title[f"Ver video {tag}"]["due_on"] == date(2026, 9, 8)
    assert by_title[f"Jugar el juego {tag}"]["due_on"] == date(2026, 9, 8)
    assert by_title[f"Entregar ensayo {tag}"]["due_on"] == date(2026, 9, 18)
    assert by_title[f"Otra cosa {tag}"]["due_on"] is None


def test_forget_keeps_user_decisions_and_reextraction_recognizes_them() -> None:
    """--force borra solo las extraídas pendientes/vencidas; una completada o descartada se queda
    con su procedencia y la re-extracción la reconoce en vez de crear otra."""
    src = _source("whatsapp", mode="programa")
    mid = _chat_inbox(src, f"{uuid4().hex[:12]}@g.us")
    tag = uuid4().hex[:4]
    _persist(
        (mid,),
        [
            TareaItem(source_inbox_ids=(mid,), title=f"Ver el video {tag}"),
            TareaItem(source_inbox_ids=(mid,), title=f"Llegar puntual {tag}"),
            TareaItem(source_inbox_ids=(mid,), title=f"Jugar el juego {tag}"),
        ],
    )
    rows = {r["title"]: r for r in _rows(mid)}
    with connection() as c:
        tareas.set_status(c, 1, rows[f"Ver el video {tag}"]["id"], "completada")
        tareas.discard(c, 1, rows[f"Llegar puntual {tag}"]["id"])
        removed = TareasModule().forget_inbox(c, 1, [mid])
    assert removed == 1  # solo «Jugar el juego» (pendiente) se borró
    kept = {r["title"]: r for r in _rows(mid)}
    assert set(kept) == {f"Ver el video {tag}", f"Llegar puntual {tag}"}
    # re-extracción: el LLM vuelve a proponer las tres
    _persist(
        (mid,),
        [
            TareaItem(source_inbox_ids=(mid,), title=f"Ver el video {tag}"),
            TareaItem(source_inbox_ids=(mid,), title=f"Llegar puntual {tag}"),
            TareaItem(source_inbox_ids=(mid,), title=f"Jugar el juego {tag}"),
        ],
    )
    after = {r["title"]: r for r in _rows(mid)}
    assert len(after) == 3
    assert after[f"Ver el video {tag}"]["status"] == "completada"
    assert after[f"Llegar puntual {tag}"]["status"] == "cancelada"
    assert after[f"Jugar el juego {tag}"]["status"] == "pendiente"


def test_parent_hint_groups_steps_under_one_project_task() -> None:
    """Dos mensajes con pasos del mismo proyecto → una tarea padre y dos pasos colgados; una nueva
    pasada reusa el padre en vez de crear otro."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    m1, m2 = _chat_inbox(src, chat), _chat_inbox(src, chat)
    tag = uuid4().hex[:4]
    _persist(
        (m1, m2),
        [
            TareaItem(
                source_inbox_ids=(m1,),
                title=f"Publicar el post del juguete {tag}",
                parent_hint=f"Proyecto Canva {tag}",
            ),
            TareaItem(
                source_inbox_ids=(m2,),
                title=f"Paso 3: post de opinión {tag}",
                parent_hint=f"proyecto canva {tag}",
            ),
            TareaItem(source_inbox_ids=(m2,), title=f"Hacer los ejercicios {tag}"),
        ],
    )
    rows = {r["title"]: r for r in _rows(m1) + _rows(m2)}
    parent_ids = {r["parent_id"] for r in rows.values() if r["parent_id"] is not None}
    assert len(parent_ids) == 1
    (pid,) = parent_ids
    assert rows[f"Publicar el post del juguete {tag}"]["parent_id"] == pid
    assert rows[f"Paso 3: post de opinión {tag}"]["parent_id"] == pid
    assert rows[f"Hacer los ejercicios {tag}"]["parent_id"] is None
    with connection() as c:
        parent = tareas.get(c, 1, pid)
    assert parent["metadata"]["is_parent"] is True and parent["asunto_id"] == curso
    # re-extracción del segundo mensaje: mismo padre, sin duplicar el paso
    _persist(
        (m2,),
        [
            TareaItem(
                source_inbox_ids=(m2,),
                title=f"Paso 3: post de opinión {tag}",
                parent_hint=f"Proyecto Canva {tag}",
            )
        ],
    )
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_tareas WHERE parent_id = :p"), {"p": pid}
        ).scalar_one()
    assert n == 2


def test_tareas_declares_its_own_llm_consumer() -> None:
    from memex.llm.settings import LLM_CONSUMERS
    from memex.modules.grouping import own_consumer

    assert own_consumer(TareasModule()) == "extract_tareas"
    assert "extract_tareas" in LLM_CONSUMERS


def test_resource_message_attaches_link_to_the_project_not_a_task() -> None:
    """La plantilla de Canva llega como mensaje que es solo un enlace: entra como `recurso` del
    proyecto (links del padre), no como tarea; y los `links` de un paso se guardan y se suman al
    repetirse."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    tag = uuid4().hex[:4]
    m_paso, m_link = _chat_inbox(src, chat), _chat_inbox(src, chat)
    _persist(
        (m_paso, m_link),
        [
            TareaItem(
                source_inbox_ids=(m_paso,),
                title=f"Publicar el post del juguete {tag}",
                parent_hint=f"Proyecto Canva {tag}",
                links=("https://youtu.be/abc", "no-es-url", "https://youtu.be/abc"),
            ),
            TareaItem(
                source_inbox_ids=(m_link,),
                title="Plantilla de Canva",
                kind="recurso",
                parent_hint=f"Proyecto Canva {tag}",
                links=("https://canva.link/rls21",),
            ),
        ],
    )
    rows = _rows(m_paso) + _rows(m_link)
    assert not any(r["title"] == "Plantilla de Canva" for r in rows)  # no es tarea
    paso = next(r for r in rows if r["title"] == f"Publicar el post del juguete {tag}")
    with connection() as c:
        parent = tareas.get(c, 1, int(paso["parent_id"]))
        paso_full = tareas.get(c, 1, int(paso["id"]))
    assert parent["metadata"]["links"] == ["https://canva.link/rls21"]
    assert m_link in [int(i) for i in parent["source_inbox_ids"]]
    assert paso_full["metadata"]["links"] == ["https://youtu.be/abc"]
    # repetir el paso con otro link: se suma, no duplica
    _persist(
        (m_paso,),
        [
            TareaItem(
                source_inbox_ids=(m_paso,),
                title=f"Publicar el post del juguete {tag}",
                parent_hint=f"Proyecto Canva {tag}",
                links=("https://youtu.be/abc", "https://sitio.edu/guia"),
            )
        ],
    )
    with connection() as c:
        again = tareas.get(c, 1, int(paso["id"]))
    assert again["metadata"]["links"] == ["https://youtu.be/abc", "https://sitio.edu/guia"]


def test_schema_links_and_kind_normalize() -> None:
    it = TareaItem(
        source_inbox_ids=(1,),
        title="x",
        links=["https://a.b/c", "ftp://no", " https://a.b/c ", "https://d.e"],
        kind="link",
    )
    assert it.links == ("https://a.b/c", "https://d.e") and it.kind == "recurso"
    assert TareaItem(source_inbox_ids=(1,), title="x").kind == "tarea"


# --- hechas desde mensajes propios y cobertura de completadas ---------------------------------- #


class _FakeJudge:
    """Juez de mentira: devuelve `same` fijo y cuenta las llamadas."""

    def __init__(self, same: bool, confidence: float = 0.9) -> None:
        self.same = same
        self.confidence = confidence
        self.calls = 0

    async def complete(self, messages: Any, **_: Any) -> Any:
        from decimal import Decimal

        from memex.llm import LLMResult, LLMUsage

        self.calls += 1
        return LLMResult(
            content=json.dumps({"same": self.same, "confidence": self.confidence, "reason": "x"}),
            model="fake-judge",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


def test_render_marks_own_chat_messages_as_yo() -> None:
    from memex.processing.render import render_payload

    mine = render_payload(
        {
            "chat_title": "Curso",
            "sender": {"phone": None, "display_name": None, "is_self": True},
            "text": "ya lo mandé",
        }
    )
    assert mine.startswith("yo (la propia persona):")
    other = render_payload(
        {
            "chat_title": "Curso",
            "sender": {"phone": "57300", "display_name": "Ana", "is_self": False},
            "text": "hola",
        }
    )
    assert other.startswith("Ana:")


def test_own_message_marks_the_task_done() -> None:
    """La foto propia de la autoevaluación llena (kind=hecha) completa la pendiente con la fecha
    del mensaje; el pedido repetido después de completarla NO vuelve a crear el pendiente."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    tag = uuid4().hex[:4]
    slide = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    mine = _chat_inbox(src, chat, when="2026-09-07 21:00:00+00")
    _persist(
        (slide, mine),
        [
            TareaItem(source_inbox_ids=(slide,), title=f"Completar la autoevaluación {tag}"),
            TareaItem(
                source_inbox_ids=(mine,),
                title=f"Completar la autoevaluación {tag}",
                kind="hecha",
                evidence="foto de la autoevaluación con las estrellas marcadas",
            ),
        ],
    )
    rows = _rows(slide)
    assert len(rows) == 1 and rows[0]["status"] == "completada"
    with connection() as c:
        full = tareas.get(c, 1, int(rows[0]["id"]))
    assert full["done_at"] is not None and full["done_at"].date() == date(2026, 9, 7)
    assert mine in [int(i) for i in full["source_inbox_ids"]]
    # la misma consigna repetida el MISMO día (título igual) → cubierta, sin juez
    again = _chat_inbox(src, chat, when="2026-09-07 22:00:00+00")
    _persist(
        (again,), [TareaItem(source_inbox_ids=(again,), title=f"Completar la autoevaluación {tag}")]
    )
    assert [r["status"] for r in _rows(again)] == ["completada"]
    # pedida al día SIGUIENTE de completarla → asignación nueva (otra clase, otra autoevaluación)
    nextday = _chat_inbox(src, chat, when="2026-09-08 20:00:00+00")
    _persist(
        (nextday,),
        [TareaItem(source_inbox_ids=(nextday,), title=f"Completar la autoevaluación {tag}")],
    )
    assert [r["status"] for r in _rows(nextday)] == ["pendiente"]


def test_completed_cover_uses_the_judge_for_similar_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.modules.tareas import extract as extract_mod

    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    tag = uuid4().hex[:4]
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (first,),
        [
            TareaItem(
                source_inbox_ids=(first,),
                title=f"Completar la autoevaluación (self-assessment) {tag}",
            )
        ],
    )
    done_id = int(_rows(first)[0]["id"])
    with connection() as c:
        tareas.set_status(c, 1, done_id, "completada")  # la persona la marcó hoy

    # juez dice «misma» → cubierta
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    similar = _chat_inbox(src, chat, when="2026-09-08 20:00:00+00")
    _persist(
        (similar,),
        [
            TareaItem(
                source_inbox_ids=(similar,),
                title=f"Completar la autoevaluación con estrellas {tag}",
            )
        ],
    )
    assert judge.calls == 1
    assert [int(r["id"]) for r in _rows(similar)] == [done_id]
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM llm_calls WHERE purpose = 'tareas_judge' AND inbox_id = :i"),
            {"i": similar},
        ).scalar_one()
    assert n == 1

    # juez dice «nueva» → pendiente nuevo
    judge2 = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge2)
    other = _chat_inbox(src, chat, when="2026-09-08 21:00:00+00")
    _persist(
        (other,),
        [
            TareaItem(
                source_inbox_ids=(other,),
                title=f"Completar la autoevaluación del proyecto final {tag}",
            )
        ],
    )
    assert judge2.calls == 1
    rows = _rows(other)
    assert len(rows) == 1 and rows[0]["status"] == "pendiente" and int(rows[0]["id"]) != done_id

    # pedida DESPUÉS de completarla (mensaje futuro) → nueva sin preguntarle al juez de COBERTURA
    # (el de pendientes sí opina sobre la pendiente parecida, y acá dice «distinta»)
    judge3 = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge3)
    later = _chat_inbox(src, chat, when="2099-01-01 20:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,), title=f"Completar la autoevaluación con estrellas {tag}"
            )
        ],
    )
    assert _rows(later)[0]["status"] == "pendiente" and int(_rows(later)[0]["id"]) != done_id
    with connection() as c:
        kinds = [
            r[0]
            for r in c.execute(
                text(
                    "SELECT metadata->>'kind' FROM llm_calls "
                    "WHERE purpose = 'tareas_judge' AND inbox_id = :i"
                ),
                {"i": later},
            )
        ]
    assert kinds == ["pending"] * judge3.calls and "cover" not in kinds


def test_tareas_judge_consumer_is_registered() -> None:
    from memex.llm.settings import LLM_CONSUMERS
    from memex.modules.tareas.judge import parse_verdict

    assert "tareas_judge" in LLM_CONSUMERS
    assert parse_verdict('{"same": true, "confidence": 0.8}').same is True
    assert parse_verdict("no json").same is False
    assert parse_verdict('[{"misma": "sí", "confidence": "0.7"}]').same is True


def test_resource_attaches_to_the_named_task_and_never_creates_a_project() -> None:
    """Un mensaje que es solo el enlace del video de una tarea ya extraída se pega a ESA tarea;
    un recurso con un nombre desconocido no crea nada."""
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    tag = uuid4().hex[:4]
    slide, link = _chat_inbox(src, chat), _chat_inbox(src, chat)
    _persist(
        (slide, link),
        [
            TareaItem(source_inbox_ids=(slide,), title=f"Ver el video y tomar notas {tag}"),
            TareaItem(
                source_inbox_ids=(link,),
                title="Video de la clase",
                kind="recurso",
                parent_hint=f"Ver el video y tomar notas {tag}",
                links=("https://youtu.be/xyz",),
            ),
            TareaItem(
                source_inbox_ids=(link,),
                title="Otro enlace",
                kind="recurso",
                parent_hint=f"Algo que no existe {tag}",
                links=("https://example.com/nada",),
            ),
        ],
    )
    rows = _rows(slide)
    assert len(rows) == 1 and rows[0]["parent_id"] is None
    with connection() as c:
        task = tareas.get(c, 1, int(rows[0]["id"]))
        n_parents = c.execute(
            text(
                "SELECT count(*) FROM mod_tareas WHERE asunto_id = :a "
                "AND (metadata->>'is_parent')::boolean IS TRUE"
            ),
            {"a": curso},
        ).scalar_one()
    assert task["metadata"]["links"] == ["https://youtu.be/xyz"]
    assert link in [int(i) for i in task["source_inbox_ids"]]
    assert n_parents == 0


def test_tasks_from_a_document_get_no_inferred_dates() -> None:
    """Un PDF subido al asunto (diapositivas) no tiene fecha de mensaje: aunque el extractor
    marque «próxima sesión», la tarea queda sin fecha (la de subida no es la de la clase)."""
    src = _source("documentos")
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [
        _chat_inbox(_source("whatsapp", mode="programa"), f"{uuid4().hex[:12]}@g.us")
        for _ in range(3)
    ]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    _recurring_class(seed[0], f"Programa Intensivo {uuid4().hex[:4]}")
    doc = _inbox(src, {"captured_from": "web", "filename": "slides.pdf", "body_text": ""})
    with connection() as c:
        asuntos_store.add_evidence(c, 1, curso, doc, method="manual", confidence=1.0)
    tag = uuid4().hex[:4]
    _persist(
        (doc,),
        [
            TareaItem(
                source_inbox_ids=(doc,),
                title=f"Escribir el ensayo {tag}",
                due_kind="proxima_sesion",
            )
        ],
    )
    (row,) = _rows(doc)
    assert row["asunto_id"] == curso and row["due_on"] is None


def test_session_index_from_name() -> None:
    from memex.modules.tareas.extract import session_index_from_name as idx

    assert idx("3.pdf") == 3
    assert idx("B1-1_10.pdf") == 10
    assert idx("Clase 7 - pasado simple.pdf") == 7
    assert idx("sesion_12.pdf") == 12
    assert idx("Lesson 4.pdf") == 4
    assert idx("B1_EFL_Grammar_and_Writing_Quiz (1).pdf") is None
    assert idx("programa.pdf") is None
    assert idx("carpeta/B1-1/2.pdf") == 2


def test_course_session_dates_and_document_dating() -> None:
    """La lámina 3 de un curso L-V desde el 2-sep es la clase del 4-sep; su «class preparation»
    vence la clase siguiente (lunes 7). Un archivo sin número queda sin fecha."""
    from memex.modules.tareas.extract import course_session_dates

    src_chat = _source("whatsapp", mode="programa")
    src_doc = _source("documentos")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src_chat, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    _recurring_class(seed[0], f"Programa Intensivo {uuid4().hex[:4]}")
    with connection() as c:
        days = course_session_dates(c, 1, curso)
    assert (
        days[0] == date(2026, 9, 2) and days[2] == date(2026, 9, 4) and days[3] == date(2026, 9, 7)
    )
    assert len(days) == 18 and days[-1] == date(2026, 9, 25)

    tag = uuid4().hex[:4]
    lamina3 = _inbox(src_doc, {"captured_from": "web", "filename": "B1-1_3.pdf", "body_text": ""})
    sin_num = _inbox(src_doc, {"captured_from": "web", "filename": "programa.pdf", "body_text": ""})
    with connection() as c:
        asuntos_store.add_evidence(c, 1, curso, lamina3, method="manual", confidence=1.0)
        asuntos_store.add_evidence(c, 1, curso, sin_num, method="manual", confidence=1.0)
    _persist(
        (lamina3, sin_num),
        [
            TareaItem(
                source_inbox_ids=(lamina3,),
                title=f"Traer el juguete {tag}",
                due_kind="proxima_sesion",
            ),
            TareaItem(
                source_inbox_ids=(sin_num,),
                title=f"Leer el programa {tag}",
                due_kind="proxima_sesion",
            ),
        ],
    )
    (r3,) = _rows(lamina3)
    assert r3["due_on"] == date(2026, 9, 7)
    with connection() as c:
        full = tareas.get(c, 1, int(r3["id"]))
    assert full["metadata"]["session_day"] == "2026-09-04"
    assert full["status"] == "vencida"  # la clase del 7 ya pasó: cierre automático
    (rp,) = _rows(sin_num)
    assert rp["due_on"] is None and rp["status"] == "pendiente"


def test_dato_item_lands_on_the_asunto_not_as_task() -> None:
    from memex.modules.asuntos.datos import datos_of

    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    msg = _chat_inbox(src, chat)
    _persist(
        (msg,),
        [
            TareaItem(
                source_inbox_ids=(msg,),
                title="Código de clase de la plataforma",
                kind="dato",
                value="QiwJN-zjIwXZ",
                evidence="Platform class code: QiwJN-zjIwXZ",
            )
        ],
    )
    assert _rows(msg) == []  # no es tarea
    with connection() as c:
        datos = datos_of(c, 1, curso)
    assert [(d["label"], d["value"], d["inbox_ids"]) for d in datos] == [
        ("Código de clase de la plataforma", "QiwJN-zjIwXZ", [msg])
    ]
    assert TareaItem(source_inbox_ids=(1,), title="x", kind="code", value=" 12 ").value == "12"


def test_secret_dato_item_is_skipped_without_error() -> None:
    from memex.modules.asuntos.datos import datos_of

    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    seed = [_chat_inbox(src, chat) for _ in range(3)]
    with connection() as c:
        for iid in seed:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    msg = _chat_inbox(src, chat)
    _persist(
        (msg,),
        [
            TareaItem(
                source_inbox_ids=(msg,), title="Usuario del campus", kind="dato", value="ana"
            ),
            TareaItem(
                source_inbox_ids=(msg,),
                title="Contraseña inicial del campus",
                kind="dato",
                value="x",
            ),
        ],
    )
    with connection() as c:
        assert [d["label"] for d in datos_of(c, 1, curso)] == ["Usuario del campus"]


# --- la misma tarea por varias vías: enriquecer, no duplicar ------------------------------------ #


def _seeded_course() -> tuple[int, str, int]:
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Intensivo {uuid4().hex[:5]}")
    with connection() as c:
        for iid in [_chat_inbox(src, chat) for _ in range(3)]:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    return src, chat, curso


def _tarea(tarea_id: int) -> dict[str, Any]:
    with connection() as c:
        return tareas.get(c, 1, tarea_id)


def test_same_task_from_another_source_enriches_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una nota de clase dice «traer papel o tablet»; al día siguiente el chat dice «llevar hojas
    para la evaluación escrita» con un detalle nuevo y prioridad alta. Parecido medio → juez dice
    «misma» → UNA tarea con las dos procedencias, el detalle sumado y la prioridad subida."""
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (first,),
        [
            TareaItem(
                source_inbox_ids=(first,),
                title="Traer papel o tablet para la evaluación de writing",
                description="Jueves y viernes.",
                due_on=date(2026, 9, 10),
            )
        ],
    )
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    later = _chat_inbox(src, chat, when="2026-09-08 09:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,),
                title="Llevar hojas para la evaluación escrita",
                description="A mano, sin laboratorio, para evitar el autocorrector.",
                due_on=date(2026, 9, 10),
                priority="alta",
            )
        ],
    )
    assert judge.calls == 1
    rows = _rows(later)
    assert len(rows) == 1 and rows[0]["id"] == _rows(first)[0]["id"]
    t = _tarea(int(rows[0]["id"]))
    assert set(t["source_inbox_ids"]) == {first, later}
    assert t["title"] == "Traer papel o tablet para la evaluación de writing"
    assert "Jueves y viernes." in t["description"] and "autocorrector" in t["description"]
    assert "(2026-09-08)" in t["description"]
    assert t["priority"] == "alta"
    assert t["due_on"] == "2026-09-10"
    assert t["metadata"]["aliases"] == ["Llevar hojas para la evaluación escrita"]


def test_judge_says_different_creates_a_second_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (first,),
        [
            TareaItem(
                source_inbox_ids=(first,),
                title="Leer el texto de la página 18",
                due_on=date(2026, 9, 10),
            )
        ],
    )
    judge = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    later = _chat_inbox(src, chat, when="2026-09-08 09:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,),
                title="Resumir el texto de la unidad 2",
                due_on=date(2026, 9, 10),
            )
        ],
    )
    assert judge.calls == 1
    assert _rows(later)[0]["id"] != _rows(first)[0]["id"]


def test_exact_title_does_not_call_the_judge_and_later_message_moves_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismo título: dedup determinista (sin juez). Un mensaje POSTERIOR con otra fecha mueve el
    plazo (quedó historial) y reabre la tarea si estaba vencida; uno ANTERIOR no la toca."""
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (first,),
        [
            TareaItem(
                source_inbox_ids=(first,),
                title="Entregar el ensayo final",
                due_on=date(2026, 9, 10),
            )
        ],
    )
    tid = int(_rows(first)[0]["id"])
    with connection() as c:
        tareas.set_status(c, 1, tid, "vencida")
    later = _chat_inbox(src, chat, when="2026-09-12 09:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,),
                title="Entregar el ensayo final",
                due_on=date(2099, 9, 19),
            )
        ],
    )
    assert judge.calls == 0
    t = _tarea(tid)
    assert t["status"] == "pendiente" and t["due_on"] == "2099-09-19"
    assert t["metadata"]["due_history"] == [
        {"from": "2026-09-10", "to": "2099-09-19", "day": "2026-09-12", "inbox_ids": [later]}
    ]
    assert set(t["source_inbox_ids"]) == {first, later}
    # Un mensaje más viejo que el que fijó la fecha no la pisa.
    older = _chat_inbox(src, chat, when="2026-09-01 09:00:00+00")
    _persist(
        (older,),
        [
            TareaItem(
                source_inbox_ids=(older,), title="Entregar el ensayo final", due_on=date(2026, 9, 5)
            )
        ],
    )
    t = _tarea(tid)
    assert t["due_on"] == "2099-09-19" and len(t["metadata"]["due_history"]) == 1
    assert older in t["source_inbox_ids"]


def test_two_wordings_in_the_same_message_are_one_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una nota larga (resumen + transcripción) pide lo mismo dos veces con otras palabras: misma
    procedencia con poco parecido léxico → juez → una sola tarea."""
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    note = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (note,),
        [
            TareaItem(
                source_inbox_ids=(note,),
                title="Traer papel o tablet para la evaluación de writing",
                due_on=date(2026, 9, 10),
            ),
            TareaItem(
                source_inbox_ids=(note,),
                title="Traer materiales para escribir",
                description="Papel o tablet, jueves y viernes.",
                due_on=date(2026, 9, 10),
            ),
        ],
    )
    assert judge.calls == 1
    rows = _rows(note)
    assert len(rows) == 1
    assert "Papel o tablet, jueves y viernes." in _tarea(int(rows[0]["id"]))["description"]


def test_unrelated_tasks_in_the_same_message_do_not_call_the_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    slide = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (slide,),
        [
            TareaItem(source_inbox_ids=(slide,), title="Ver el video indicado"),
            TareaItem(source_inbox_ids=(slide,), title="Leer el texto de la página 18"),
            TareaItem(
                source_inbox_ids=(slide,), title="Escribir un párrafo sobre la primera visita"
            ),
        ],
    )
    assert judge.calls == 0
    assert len(_rows(slide)) == 3


def test_discarded_task_is_not_recreated_nor_enriched(monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.modules.tareas import extract as extract_mod

    src, chat, _curso = _seeded_course()
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist((first,), [TareaItem(source_inbox_ids=(first,), title="Llegar puntual a clase")])
    tid = int(_rows(first)[0]["id"])
    with connection() as c:
        tareas.discard(c, 1, tid)
    later = _chat_inbox(src, chat, when="2026-09-08 09:00:00+00")
    _persist(
        (later,),
        [TareaItem(source_inbox_ids=(later,), title="Llegar puntual a clase", description="x")],
    )
    t = _tarea(tid)
    assert t["status"] == "cancelada" and t["description"] == ""
    assert later not in t["source_inbox_ids"]
    assert _rows(later) == []


# --- proyectos: sin fantasmas ------------------------------------------------------------------- #


def _project_with_step(src: int, chat: str) -> tuple[int, int, int]:
    """Proyecto «Proyecto de Instagram en Canva» con dos pasos (un proyecto nace con dos o más).
    Devuelve (mensaje, primer paso, proyecto)."""
    m = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (m,),
        [
            TareaItem(
                source_inbox_ids=(m,),
                title="Crear un post de opinión sobre la situación política en Colombia",
                parent_hint="Proyecto de Instagram en Canva",
            ),
            TareaItem(
                source_inbox_ids=(m,),
                title="Copiar la plantilla de perfil",
                parent_hint="Proyecto de Instagram en Canva",
            ),
        ],
    )
    # el proyecto tambien lleva el mensaje en su procedencia: los pasos son las filas CON padre
    step = next(r for r in _rows(m) if r["parent_id"] is not None and "opinión" in r["title"])
    return m, int(step["id"]), int(step["parent_id"])


def _live_projects(asunto_id: int) -> list[str]:
    with connection() as c:
        return [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT title FROM mod_tareas WHERE asunto_id = :a AND status <> 'cancelada' "
                    "AND (metadata->>'is_parent')::boolean IS TRUE ORDER BY id"
                ),
                {"a": asunto_id},
            )
        ]


def test_step_matched_to_an_existing_one_does_not_leave_a_phantom_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La nota de clase nombra el proyecto de otra forma («Proyecto de video») y repite un paso con
    otras palabras: el juez dice «mismo paso» → el paso se enriquece y NO nace un proyecto vacío."""
    from memex.modules.tareas import extract as extract_mod

    src, chat, curso = _seeded_course()
    _m, step_id, project_id = _project_with_step(src, chat)
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    note = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (note,),
        [
            TareaItem(
                source_inbox_ids=(note,),
                title="Escribir el tercer post sobre la situación política de Colombia",
                description="Al menos 3 expresiones de opinión.",
                parent_hint="Proyecto de video",
            )
        ],
    )
    assert judge.calls == 1
    assert _live_projects(curso) == ["Proyecto de Instagram en Canva"]
    t = _tarea(step_id)
    assert t["parent_id"] == project_id and "3 expresiones" in t["description"]


def test_generic_project_name_reuses_the_only_live_project() -> None:
    src, chat, curso = _seeded_course()
    _m, _step_id, project_id = _project_with_step(src, chat)
    later = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,),
                title="Grabar el audio de la presentación",
                parent_hint="Proyecto en curso",
            ),
            TareaItem(
                source_inbox_ids=(later,),
                title="Diseñar la portada",
                parent_hint="el proyecto final",
            ),
        ],
    )
    assert _live_projects(curso) == ["Proyecto de Instagram en Canva"]
    assert {r["parent_id"] for r in _rows(later)} == {project_id}


def test_specific_project_name_that_does_not_match_creates_a_second_project() -> None:
    src, chat, curso = _seeded_course()
    _project_with_step(src, chat)
    later = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (later,),
        [
            TareaItem(
                source_inbox_ids=(later,),
                title="Grabar la entrevista",
                parent_hint="Proyecto de video final",
            ),
            TareaItem(
                source_inbox_ids=(later,),
                title="Editar el video",
                parent_hint="Proyecto de video final",
            ),
        ],
    )
    assert _live_projects(curso) == ["Proyecto de Instagram en Canva", "Proyecto de video final"]


def test_a_single_step_does_not_create_a_project_until_a_second_one_names_it() -> None:
    """«Homework: …» o «Tarea de la semana» como `parent_hint` de una sola tarea creaba un
    «proyecto» de un paso, repetido y vacío a la vista. Un proyecto nace con dos pasos: el primero
    queda suelto recordando el nombre, y se cuelga solo cuando llega el segundo."""
    src, chat, curso = _seeded_course()
    first = _chat_inbox(src, chat, when="2026-09-07 20:00:00+00")
    _persist(
        (first,),
        [
            TareaItem(
                source_inbox_ids=(first,),
                title="Ver el video sobre present perfect continuous",
                parent_hint="Homework: Present perfect continuous",
            )
        ],
    )
    assert _live_projects(curso) == []
    [loose] = _rows(first)
    assert loose["parent_id"] is None
    hint = _tarea(int(loose["id"]))["metadata"]["parent_hint"]
    assert hint == "Homework: Present perfect continuous"

    second = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist(
        (second,),
        [
            TareaItem(
                source_inbox_ids=(second,),
                title="Escribir cinco oraciones en present perfect continuous",
                parent_hint="Homework: present perfect continuous",
            )
        ],
    )
    assert _live_projects(curso) == ["Homework: present perfect continuous"]
    step2 = next(r for r in _rows(second) if r["parent_id"] is not None)
    assert _tarea(int(loose["id"]))["parent_id"] == step2["parent_id"]


# --- dinámicas de programa ---------------------------------------------------------------------- #


def test_item_schema_tolera_puntos_y_due_kind_en_texto() -> None:
    it = TareaItem(
        source_inbox_ids=(1,), title="Descifrar la frase", points="70 puntos", due_kind="Mismo día"
    )
    assert it.points == 70 and it.due_kind == "mismo_dia"
    it2 = TareaItem(source_inbox_ids=(1,), title="x", points=None, due_kind="una semana")
    assert it2.points is None and it2.due_kind == "una_semana"
    assert TareaItem(source_inbox_ids=(1,), title="x", points="sin puntos").points is None


def test_dinamica_de_chat_vence_el_mismo_dia_o_a_la_semana_y_guarda_puntos() -> None:
    """Las dinámicas de puntos de un programa llegaban sin fecha (pendientes para siempre) y sin
    los puntos. Ahora: «al primero que…» vence el mismo día del mensaje, una consigna sin plazo
    vence a la semana, y los puntos quedan en metadata (marcan la tarea como dinámica)."""
    src, chat, _curso = _seeded_course()
    m = _chat_inbox(src, chat, when="2026-09-08 20:00:00+00")
    _persist(
        (m,),
        [
            TareaItem(
                source_inbox_ids=(m,),
                title="Descifrar el acertijo y enviar la frase",
                due_kind="mismo_dia",
                points=70,
            ),
            TareaItem(
                source_inbox_ids=(m,),
                title="Publicar la experiencia con el hashtag del programa",
                due_kind="una_semana",
                points=40,
            ),
            TareaItem(
                source_inbox_ids=(m,),
                title="Compartir el enlace de la publicación",
                due_on=date(2026, 9, 12),
            ),
        ],
    )
    rows = {r["title"]: r for r in _rows(m)}
    assert str(rows["Descifrar el acertijo y enviar la frase"]["due_on"]) == "2026-09-08"
    assert (
        str(rows["Publicar la experiencia con el hashtag del programa"]["due_on"]) == "2026-09-15"
    )
    assert str(rows["Compartir el enlace de la publicación"]["due_on"]) == "2026-09-12"
    meta = _tarea(int(rows["Descifrar el acertijo y enviar la frase"]["id"]))["metadata"]
    assert meta["puntos"] == 70 and meta["dinamica"] is True
    assert meta["due_kind"] == "mismo_dia" and meta["due_source"] == "mismo_dia"
    meta2 = _tarea(int(rows["Compartir el enlace de la publicación"]["id"]))["metadata"]
    assert "puntos" not in meta2 and "dinamica" not in meta2
