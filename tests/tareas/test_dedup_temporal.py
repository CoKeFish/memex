"""Regla temporal del dedup: la misma consigna con las mismas palabras, pedida DESPUÉS de que
venció la existente y sin fecha propia, no se funde a ciegas (va al juez con las fechas); con
fecha propia sigue siendo la misma entrega con el plazo movido (determinista, sin juez)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store as asuntos_store
from memex.modules.tareas import extract as extract_mod
from memex.modules.tareas import module as tareas
from memex.modules.tareas.module import find_duplicate
from memex.modules.tareas.schema import TareaItem
from tests.tareas.test_extract import _asunto, _chat_inbox, _FakeJudge, _persist, _rows, _source

CONSIGNA = "Revisar el sitio web indicado y tomar notas"


def _curso() -> tuple[int, str, int]:
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    curso = _asunto(f"Curso {uuid4().hex[:5]}")
    with connection() as c:
        for iid in [_chat_inbox(src, chat) for _ in range(3)]:
            asuntos_store.add_evidence(c, 1, curso, iid, method="manual", confidence=1.0)
    return src, chat, curso


def _tarea(tid: int) -> dict[str, Any]:
    with connection() as c:
        return tareas.get(c, 1, tid)


def test_find_duplicate_respeta_el_tiempo() -> None:
    _, _, curso = _curso()
    with connection() as c:
        t = tareas.add(c, 1, title=CONSIGNA, asunto=curso, due_on=date(2026, 9, 4))
        # antes de vencer: título igual = misma
        d = find_duplicate(
            c, 1, title=CONSIGNA, asunto_id=curso, due_on=None, message_day=date(2026, 9, 3)
        )
        assert d is not None and d.matched_by == "exact" and int(d.tarea["id"]) == t["id"]
        # después de vencida y sin fecha propia: no decide solo
        assert (
            find_duplicate(
                c, 1, title=CONSIGNA, asunto_id=curso, due_on=None, message_day=date(2026, 9, 9)
            )
            is None
        )
        # después de vencida pero con fecha propia (prórroga): misma
        d2 = find_duplicate(
            c,
            1,
            title=CONSIGNA,
            asunto_id=curso,
            due_on=date(2026, 9, 12),
            message_day=date(2026, 9, 9),
            explicit_due=True,
        )
        assert d2 is not None and int(d2.tarea["id"]) == t["id"]
        # sin día del mensaje (alta manual, CLI) todo sigue igual
        d3 = find_duplicate(c, 1, title=CONSIGNA, asunto_id=curso, due_on=None)
        assert d3 is not None and int(d3.tarea["id"]) == t["id"]


def test_la_consigna_repetida_en_otra_sesion_es_otra_tarea(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sesión 2 pide «revisar el sitio web y tomar notas» (vence el 4); la sesión 6 (día 9) lo pide
    con las mismas palabras y otro material: el juez la ve con las fechas y dice que es otra."""
    judge = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    src, chat, _ = _curso()
    m2 = _chat_inbox(src, chat, when="2026-09-03 20:00:00+00")
    _persist((m2,), [TareaItem(source_inbox_ids=(m2,), title=CONSIGNA, due_on=date(2026, 9, 4))])
    first = int(_rows(m2)[0]["id"])
    m6 = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist((m6,), [TareaItem(source_inbox_ids=(m6,), title=CONSIGNA)])
    assert judge.calls == 1  # fue al juez, no se fundió a ciegas
    rows = _rows(m6)
    assert len(rows) == 1 and int(rows[0]["id"]) != first
    assert set(_tarea(first)["source_inbox_ids"]) == {m2}  # la vieja no absorbió la lámina nueva


def test_el_recordatorio_de_la_misma_consigna_si_se_funde(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismo caso, pero el juez dice que es la misma (un recordatorio): se funde como siempre."""
    judge = _FakeJudge(same=True)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    src, chat, _ = _curso()
    m2 = _chat_inbox(src, chat, when="2026-09-03 20:00:00+00")
    _persist((m2,), [TareaItem(source_inbox_ids=(m2,), title=CONSIGNA, due_on=date(2026, 9, 4))])
    first = int(_rows(m2)[0]["id"])
    m6 = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    _persist((m6,), [TareaItem(source_inbox_ids=(m6,), title=CONSIGNA)])
    assert judge.calls == 1
    assert [int(r["id"]) for r in _rows(m6)] == [first]


def test_la_prorroga_con_fecha_propia_no_pasa_por_el_juez(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = _FakeJudge(same=False)
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: judge)
    src, chat, _ = _curso()
    m2 = _chat_inbox(src, chat, when="2026-09-03 20:00:00+00")
    _persist((m2,), [TareaItem(source_inbox_ids=(m2,), title=CONSIGNA, due_on=date(2026, 9, 4))])
    first = int(_rows(m2)[0]["id"])
    with connection() as c:
        c.execute(text("UPDATE mod_tareas SET status = 'vencida' WHERE id = :i"), {"i": first})
    m6 = _chat_inbox(src, chat, when="2026-09-09 20:00:00+00")
    # fecha vigente (en el futuro): así reabre; una prórroga ya pasada no revive nada
    _persist((m6,), [TareaItem(source_inbox_ids=(m6,), title=CONSIGNA, due_on=date(2099, 9, 12))])
    assert judge.calls == 0
    t = _tarea(first)
    assert t["status"] == "pendiente" and str(t["due_on"]) == "2099-09-12"
    assert set(t["source_inbox_ids"]) == {m2, m6}


def test_la_candidata_de_la_misma_sesion_va_antes_que_la_del_titulo_calcado() -> None:
    """La lámina de la sesión 6 («revisar el sitio web y tomar notas», para el 10) se parece más
    en palabras a la lámina de la sesión 7 (mismo título, para el 11) que a la foto del tablero de
    su propia sesión («revisar el sitio sobre used to», para el 10). Al juez va primero la de la
    misma sesión / mismo plazo; una tarea vieja sin marca de sesión cuenta por el día de su
    mensaje."""
    from memex.modules.tareas.module import find_similar_candidates

    src, chat, curso = _curso()
    foto = _chat_inbox(src, chat, when="2026-09-09 21:00:00+00")
    lam7 = _chat_inbox(src, chat, when="2026-09-10 21:00:00+00")
    with connection() as c:
        mismo_dia = tareas.add(
            c,
            1,
            title="Revisar el sitio sobre used to y tomar notas",
            asunto=curso,
            due_on=date(2026, 9, 10),
            source_inbox_ids=[foto],
        )
        calcada = tareas.add(
            c,
            1,
            title="Revisar el sitio web y tomar notas",
            asunto=curso,
            due_on=date(2026, 9, 11),
            source_inbox_ids=[lam7],
        )
        cands = find_similar_candidates(
            c,
            1,
            title="Revisar el sitio web y tomar notas",
            asunto_id=curso,
            due_on=date(2026, 9, 10),
            fixed_day=date(2026, 9, 9),
        )
        assert [int(x.tarea["id"]) for x in cands][:2] == [mismo_dia["id"], calcada["id"]]
        assert cands[0].same_due and cands[0].same_session and not cands[1].same_session
