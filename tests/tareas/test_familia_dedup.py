"""Dedup de tareas por FAMILIA de asuntos (el asunto y los relacionados por `parte_de` /
`requisito_de` / `continua`): las gemelas del asunto hermano llegan al juez (`other_asunto`), una
completada del hermano cubre, la búsqueda por título ve la familia; el nombre que da el modelo
manda sobre el chat dominante cuando apunta a un asunto de esa familia; y la confirmación del
organizador (`kind='hecha'`) completa la viva más parecida por significado si el juez afirma."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store as asuntos_store
from memex.modules.tareas import extract as extract_mod
from memex.modules.tareas import module as tareas
from memex.modules.tareas.extract import resolve_asunto_for
from memex.modules.tareas.module import (
    STATUS_COMPLETADA,
    family_of,
    find_completed_cover,
    find_live_semantic,
    find_similar_candidates,
    find_task_by_title,
    set_status,
)
from memex.modules.tareas.schema import TareaItem
from memex.semantic.embedder import FixedEmbedder, use_embedder
from tests.tareas.test_extract import (
    _asunto,
    _chat_inbox,
    _FakeJudge,
    _inbox,
    _persist,
    _rows,
    _source,
)

VIDEO = "Grabar y publicar el video de 60 segundos"
PREPARAR = "Preparar el video de postulación"
CEDULA = "Renovar la cédula"
V = {
    VIDEO.lower(): [1.0, 0.9, 0.0],
    PREPARAR.lower(): [0.9, 1.0, 0.0],
    CEDULA.lower(): [0.0, 0.0, 1.0],
}


def _familia() -> tuple[int, int, int]:
    """Programa (padre), beca (parte del programa) y un asunto ajeno."""
    u = uuid4().hex[:5]
    p, ch, x = _asunto(f"Programa {u}"), _asunto(f"Beca {u}"), _asunto(f"Otro {u}")
    with connection() as c:
        assert asuntos_store.add_relation(c, 1, ch, p, "parte_de")
    return p, ch, x


def test_family_of_es_el_asunto_y_sus_relacionados_vivos() -> None:
    p, ch, x = _familia()
    with connection() as c:
        assert family_of(c, 1, p) == [p, ch]
        assert family_of(c, 1, ch) == [ch, p]
        assert family_of(c, 1, x) == [x]
        assert family_of(c, 1, None) == []
        c.execute(
            text("UPDATE mod_asuntos SET merged_into_id = :x WHERE id = :p"), {"x": x, "p": p}
        )
        assert family_of(c, 1, ch) == [ch]  # un relacionado fundido no cuenta


def test_las_gemelas_del_asunto_hermano_llegan_al_juez_marcadas() -> None:
    p, ch, _ = _familia()
    with use_embedder(FixedEmbedder(V, dim=3)), connection() as c:
        video = tareas.add(c, 1, title=VIDEO, asunto=p)
        tareas.add(c, 1, title=CEDULA, asunto=p)
        cands = find_similar_candidates(c, 1, title=PREPARAR, asunto_id=ch, due_on=None)
        assert [(int(x.tarea["id"]), x.semantic, x.other_asunto) for x in cands] == [
            (video["id"], True, True)
        ]
        # por título también ve la familia (misma obligación con las mismas palabras)
        found = find_task_by_title(
            c, 1, title=VIDEO, asunto_id=ch, statuses=("pendiente", "vencida")
        )
        assert found is not None and int(found["id"]) == video["id"]
        # y una completada del hermano cubre lo que se pide después en la beca
        set_status(c, 1, video["id"], STATUS_COMPLETADA, when=datetime(2026, 9, 18, tzinfo=UTC))
        cover = find_completed_cover(c, 1, title=VIDEO, asunto_id=ch, message_day=date(2026, 9, 17))
        assert cover is not None and cover.exact and int(cover.tarea["id"]) == video["id"]


def test_fuera_de_la_familia_no_se_compara() -> None:
    _, _, x = _familia()
    y = _asunto(f"Ajeno {uuid4().hex[:5]}")
    with use_embedder(FixedEmbedder(V, dim=3)), connection() as c:
        tareas.add(c, 1, title=VIDEO, asunto=x)
        assert find_similar_candidates(c, 1, title=PREPARAR, asunto_id=y, due_on=None) == []
        assert find_live_semantic(c, 1, title=PREPARAR, asunto_id=y) is None


def test_el_nombre_del_modelo_manda_sobre_el_chat_si_apunta_a_la_familia() -> None:
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    u = uuid4().hex[:5]
    p, ch, _ = _asunto(f"Programa {u}"), _asunto(f"Beca {u}"), _asunto(f"Otro {u}")
    with connection() as c:
        asuntos_store.add_relation(c, 1, ch, p, "parte_de")
        for iid in [_chat_inbox(src, chat) for _ in range(3)]:
            asuntos_store.add_evidence(c, 1, p, iid, method="manual", confidence=1.0)
    m = _chat_inbox(src, chat, when="2026-09-17 20:00:00+00")
    with connection() as c:
        assert resolve_asunto_for(c, 1, [m], "") == p  # el chat dominante
        assert resolve_asunto_for(c, 1, [m], f"Beca {u}") == ch  # nombra a la beca: es de ella
        assert resolve_asunto_for(c, 1, [m], f"Otro {u}") == p  # ajeno a la familia: el chat


def test_la_confirmacion_del_organizador_completa_la_viva_por_significado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    p, ch, _ = _familia()
    src = _source("imap")
    m = _inbox(src, {"subject": "You're on the list for HackLisboa 2026"})
    existing = "Inscribirte en el HackLisboa y elegir track"
    confirmed = "Registrarse en HackLisboa"
    vectors = {existing.lower(): [1.0, 0.9, 0.0], confirmed.lower(): [0.9, 1.0, 0.0]}
    with use_embedder(FixedEmbedder(vectors, dim=3)):
        with connection() as c:
            pend = tareas.add(c, 1, title=existing, asunto=ch)
            asuntos_store.add_evidence(c, 1, p, m, method="manual", confidence=1.0)
        _persist(
            (m,),
            [
                TareaItem(
                    source_inbox_ids=(m,),
                    title=confirmed,
                    kind="hecha",
                    evidence="You're on the list",
                )
            ],
        )
    rows = _rows(m)
    assert [(int(r["id"]), r["status"]) for r in rows] == [(pend["id"], "completada")]
    assert judge.calls == 1
    # sin juez que afirme, no se toca la viva: se registra la confirmación aparte, ya hecha
    judge2 = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge2)
    m2 = _inbox(src, {"subject": "Confirmación"})
    with use_embedder(FixedEmbedder(vectors, dim=3)):
        with connection() as c:
            other = tareas.add(c, 1, title=existing + " B", asunto=ch)
            asuntos_store.add_evidence(c, 1, p, m2, method="manual", confidence=1.0)
        _persist(
            (m2,),
            [TareaItem(source_inbox_ids=(m2,), title=confirmed, kind="hecha", evidence="ok")],
        )
    rows2 = _rows(m2)
    assert [r["status"] for r in rows2] == ["completada"] and int(rows2[0]["id"]) != other["id"]


def test_la_confirmacion_repetida_no_registra_una_segunda_completada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El paso ya estaba HECHO con otras palabras (marcado a mano o por una confirmación previa):
    la cobertura de completadas + juez suma la procedencia en vez de crear otra completada."""
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    p, ch, _ = _familia()
    src = _source("imap")
    existing = "Inscribirte en el HackLisboa y elegir track"
    confirmed = "Registrarse en HackLisboa"
    vectors = {existing.lower(): [1.0, 0.9, 0.0], confirmed.lower(): [0.9, 1.0, 0.0]}
    with use_embedder(FixedEmbedder(vectors, dim=3)):
        with connection() as c:
            done = tareas.add(c, 1, title=existing, asunto=ch)
            set_status(c, 1, done["id"], STATUS_COMPLETADA, when=datetime.now(UTC))
        m = _inbox(src, {"subject": "You're on the list"})
        with connection() as c:
            asuntos_store.add_evidence(c, 1, p, m, method="manual", confidence=1.0)
        _persist(
            (m,),
            [
                TareaItem(
                    source_inbox_ids=(m,), title=confirmed, kind="hecha", evidence="on the list"
                )
            ],
        )
    rows = _rows(m)
    assert [(int(r["id"]), r["status"]) for r in rows] == [(done["id"], "completada")]
    assert judge.calls == 1
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_tareas WHERE user_id = 1 AND asunto_id = ANY(:a)"),
            {"a": [p, ch]},
        ).scalar_one()
    assert n == 1  # ninguna segunda completada


def test_la_cobertura_por_paso_del_plan_elige_el_mas_parecido_no_el_mas_reciente() -> None:
    """Dos pasos completados del plan, ninguno sobre el piso semántico: la cobertura propone el
    más parecido por significado (el que la confirmación cumple), no el completado más tarde."""
    _, ch, _ = _familia()
    postular = "Postularse a HackLisboa"
    inscribir = "Inscribirte en HackLisboa y elegir track"
    formulario = "Llenar el formulario de la beca"
    vectors = {
        postular.lower(): [1.0, 0.0, 0.0],
        inscribir.lower(): [0.5, 0.866, 0.0],  # coseno 0,5: bajo el piso, pero el más cercano
        formulario.lower(): [0.2, 0.0, 0.98],  # coseno 0,2
    }
    with use_embedder(FixedEmbedder(vectors, dim=3)), connection() as c:
        proj = tareas.add(c, 1, title="Postulación a la beca", asunto=ch)
        s1 = tareas.add(c, 1, title=inscribir, asunto=ch, parent=proj["id"])
        s2 = tareas.add(c, 1, title=formulario, asunto=ch, parent=proj["id"])
        # mediodía UTC: el mismo día también en hora local (a medianoche UTC sería el 19 en Bogotá
        # y quedaría «completada antes de pedirse»)
        set_status(c, 1, s1["id"], STATUS_COMPLETADA, when=datetime(2026, 9, 20, 12, tzinfo=UTC))
        set_status(c, 1, s2["id"], STATUS_COMPLETADA, when=datetime(2026, 9, 25, 12, tzinfo=UTC))
        cover = find_completed_cover(
            c, 1, title=postular, asunto_id=ch, message_day=date(2026, 9, 20)
        )
        assert cover is not None and not cover.exact and not cover.semantic
        assert int(cover.tarea["id"]) == s1["id"]
        # y como candidatas de una pendiente nueva, el paso más parecido va primero
        set_status(c, 1, s1["id"], "pendiente")
        set_status(c, 1, s2["id"], "pendiente")
        cands = find_similar_candidates(c, 1, title=postular, asunto_id=ch, due_on=None)
        assert [int(x.tarea["id"]) for x in cands if x.plan_step][:2] == [s1["id"], s2["id"]]
