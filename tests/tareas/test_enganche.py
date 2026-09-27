"""Tareas que se cuelgan de su asunto DESPUÉS de extraídas (una nota sin chat): al engancharlas
reciben la fecha de la próxima sesión y pasan por el re-chequeo de duplicados con el juez; y dos
tareas ya registradas se pueden fundir a mano.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMResult, LLMUsage
from memex.modules.tareas import extract as extract_mod
from memex.modules.tareas import module as tareas
from memex.modules.tareas.cli import main as tareas_main
from tests.asuntos._seed import asunto


class _JuezQueFunde:
    async def complete(self, messages: Any, **_: Any) -> LLMResult:
        return LLMResult(
            content=json.dumps({"same": True, "confidence": 0.95, "reason": "mismo video"}),
            model="fake-judge",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


def _row(tid: int) -> dict[str, Any]:
    with connection() as c:
        return tareas.get(c, 1, tid)


# --------------------------------------------------------------------- enganche


def test_al_engancharse_se_fecha_y_queda_marcada(monkeypatch: pytest.MonkeyPatch) -> None:
    """La nota no tenía asunto al extraerse; al colgarla del curso, la tarea «para la próxima
    clase» recibe la fecha (horario del curso) y queda marcada para el re-chequeo."""
    monkeypatch.setattr(extract_mod, "_message_day", lambda conn, ids: date(2026, 9, 16))
    monkeypatch.setattr(
        extract_mod, "_next_session", lambda conn, uid, aid, after: date(2026, 9, 17)
    )
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        suelta = tareas.add(
            c,
            1,
            title="Ver el video y responder las preguntas",
            origin="extraction",
            source_inbox_ids=[14328],
            metadata={"due_kind": "proxima_sesion"},
        )
        con_fecha = tareas.add(
            c,
            1,
            title="Traer el trabajo impreso",
            origin="extraction",
            source_inbox_ids=[14328],
            due_on=date(2026, 9, 20),
            metadata={"due_kind": "proxima_sesion"},
        )
        n = tareas.adopt_orphan_tareas(c, 1, aid, 14328)
    assert n == 2
    a = _row(int(suelta["id"]))
    assert a["asunto_id"] == aid and a["due_on"] == "2026-09-17"
    assert (
        a["metadata"]["dedup_pending"] is True and a["metadata"]["due_source"] == "proxima_sesion"
    )
    b = _row(int(con_fecha["id"]))
    assert b["due_on"] == "2026-09-20"  # la fecha que ya tenía no se pisa
    assert b["metadata"]["dedup_pending"] is True


def test_sin_horario_no_hay_fecha_pero_si_marca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_mod, "_message_day", lambda conn, ids: date(2026, 9, 16))
    monkeypatch.setattr(extract_mod, "_next_session", lambda conn, uid, aid, after: None)
    aid = asunto("Curso sin horario", kind="programa")
    with connection() as c:
        t = tareas.add(
            c,
            1,
            title="Leer el capítulo",
            origin="extraction",
            source_inbox_ids=[5],
            metadata={"due_kind": "proxima_sesion"},
        )
        tareas.adopt_orphan_tareas(c, 1, aid, 5)
    r = _row(int(t["id"]))
    assert r["due_on"] is None and r["metadata"]["dedup_pending"] is True


def _exists(tid: int) -> bool:
    with connection() as c:
        return bool(c.execute(text("SELECT 1 FROM mod_tareas WHERE id = :t"), {"t": tid}).scalar())


def test_al_engancharse_el_proyecto_suelto_se_funde_con_el_del_asunto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso real (2026-09-17): la nota de clase se extrajo antes de saber el curso; nació un
    «Proyecto de Instagram» suelto con dos pasos y, al colgarse del curso, quedó repetido al lado
    del «Proyecto de Instagram en Canva (My Social Life)» de nueve pasos."""
    monkeypatch.setattr(extract_mod, "_message_day", lambda conn, ids: date(2026, 9, 16))
    monkeypatch.setattr(extract_mod, "_next_session", lambda conn, uid, aid, after: None)
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        canva = tareas.find_or_create_parent(
            c, 1, title="Proyecto de Instagram en Canva (My Social Life)", asunto_id=aid
        )
        tareas.add(
            c,
            1,
            title="Crear un post en Canva sobre tu foto",
            asunto=aid,
            parent=canva,
            origin="extraction",
            source_inbox_ids=[12586],
        )
        suelto = tareas.find_or_create_parent(
            c, 1, title="Proyecto de Instagram", asunto_id=None, source_inbox_ids=[14328]
        )
        tareas.attach_links(c, 1, suelto, ["https://www.canva.com/plantilla"])
        paso = tareas.add(
            c,
            1,
            title="Traer notas o trabajo impreso del proyecto de Instagram",
            parent=suelto,
            origin="extraction",
            source_inbox_ids=[14328],
        )
        n = tareas.adopt_orphan_tareas(c, 1, aid, 14328)
    assert n == 2
    assert not _exists(suelto)
    p = _row(int(paso["id"]))
    assert p["asunto_id"] == aid and p["parent_id"] == canva
    assert p["metadata"]["dedup_pending"] is True
    s = _row(canva)
    assert "Proyecto de Instagram" in s["metadata"]["aliases"]
    assert "https://www.canva.com/plantilla" in s["metadata"]["links"]
    assert 14328 in s["source_inbox_ids"]


def test_cli_dedup_parents_funde_repetidos_y_aplana_los_de_un_paso(capsys: Any) -> None:
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        canva = tareas.find_or_create_parent(
            c, 1, title="Proyecto de Instagram en Canva (My Social Life)", asunto_id=aid
        )
        for t in ("Crear un post en Canva", "Copiar la plantilla de perfil"):
            tareas.add(c, 1, title=t, asunto=aid, parent=canva, origin="extraction")
        # repetido: nació suelto y se enganchó sin fundirse (antes de la regla)
        rep = tareas.add(
            c,
            1,
            title="Proyecto de Instagram",
            asunto=aid,
            metadata={"is_parent": True},
            force=True,
            origin="extraction",
        )
        paso_rep = tareas.add(
            c,
            1,
            title="Traer notas impresas",
            asunto=aid,
            parent=int(rep["id"]),
            origin="extraction",
        )
        # «proyecto» de un solo paso y sin enlaces: no es un proyecto
        hw = tareas.add(
            c,
            1,
            title="Homework: Present perfect continuous",
            asunto=aid,
            metadata={"is_parent": True},
            force=True,
            origin="extraction",
        )
        video = tareas.add(
            c, 1, title="Ver el video", asunto=aid, parent=int(hw["id"]), origin="extraction"
        )
    assert tareas_main(["dedup-parents", "--asunto", str(aid)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "Proyecto de Instagram" in out
    assert _exists(int(rep["id"])) and _exists(int(hw["id"]))  # sin --apply no toca nada

    assert tareas_main(["dedup-parents", "--asunto", str(aid), "--apply", "--flatten-singles"]) == 0
    out = capsys.readouterr().out
    assert "1 proyecto" in out and "aplanado" in out
    assert not _exists(int(rep["id"])) and not _exists(int(hw["id"]))
    assert _row(int(paso_rep["id"]))["parent_id"] == canva
    v = _row(int(video["id"]))
    assert v["parent_id"] is None
    assert v["metadata"]["parent_hint"] == "Homework: Present perfect continuous"


# --------------------------------------------------------------------- fusión


def test_merge_funde_y_deja_la_absorbida_cancelada() -> None:
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        proyecto = tareas.find_or_create_parent(c, 1, title="Homework", asunto_id=aid)
        queda = tareas.add(
            c,
            1,
            title="Ver el video sobre present perfect continuous",
            asunto=aid,
            parent=proyecto,
            due_on=date(2026, 9, 17),
            origin="extraction",
            source_inbox_ids=[14319],
            metadata={"links": ["https://youtu.be/x"]},
        )
        se_va = tareas.add(
            c,
            1,
            title="Ver el video y responder las preguntas asignadas",
            description="Video sobre Present Perfect vs. Present Perfect Continuous.",
            asunto=aid,
            priority="alta",
            origin="extraction",
            source_inbox_ids=[14328],
            metadata={"aliases": ["Responder las preguntas del video"], "links": ["https://n/1"]},
        )
        changes = tareas.merge_tareas(c, 1, into=int(queda["id"]), absorb=int(se_va["id"]))
    assert "detalle" in changes and "prioridad alta" in changes and "enlaces +1" in changes
    q = _row(int(queda["id"]))
    assert q["status"] == "pendiente" and q["due_on"] == "2026-09-17"
    assert q["priority"] == "alta"
    assert sorted(q["source_inbox_ids"]) == [14319, 14328]
    assert set(q["metadata"]["aliases"]) == {
        "Ver el video y responder las preguntas asignadas",
        "Responder las preguntas del video",
    }
    assert set(q["metadata"]["links"]) == {"https://youtu.be/x", "https://n/1"}
    s = _row(int(se_va["id"]))
    assert s["status"] == "cancelada" and s["metadata"]["merged_into"] == int(queda["id"])
    with connection() as c:
        with pytest.raises(ValueError):
            tareas.merge_tareas(c, 1, into=int(queda["id"]), absorb=int(queda["id"]))
        with pytest.raises(ValueError):
            tareas.merge_tareas(c, 1, into=int(queda["id"]), absorb=proyecto)  # un proyecto no
        with pytest.raises(ValueError):  # al revés: la cancelada no puede quedar
            tareas.merge_tareas(c, 1, into=int(se_va["id"]), absorb=int(queda["id"]))


# --------------------------------------------------------------------- re-chequeo


def _gemelas() -> tuple[int, int, int]:
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        vieja = tareas.add(
            c,
            1,
            title="Ver el video sobre present perfect continuous",
            asunto=aid,
            due_on=date(2026, 9, 17),
            origin="extraction",
            source_inbox_ids=[14319],
        )
        nueva = tareas.add(
            c,
            1,
            title="Ver el video y responder las preguntas asignadas",
            asunto=aid,
            origin="extraction",
            source_inbox_ids=[14328],
            metadata={"dedup_pending": True},
        )
        otra = tareas.add(
            c, 1, title="Pagar la matrícula", asunto=aid, origin="extraction", source_inbox_ids=[1]
        )
    return int(vieja["id"]), int(nueva["id"]), int(otra["id"])


def test_recheck_funde_cuando_el_juez_dice_que_es_la_misma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: _JuezQueFunde())
    vieja, nueva, otra = _gemelas()
    st = asyncio.run(extract_mod.recheck_duplicates(1))
    assert st.revisadas == 1 and st.juzgadas >= 1 and st.fundidas == 1
    assert st.pares[0]["tarea_id"] == nueva and st.pares[0]["into"] == vieja
    assert _row(nueva)["status"] == "cancelada"
    assert _row(nueva)["metadata"].get("dedup_pending") in (None, False)
    v = _row(vieja)
    assert "Ver el video y responder las preguntas asignadas" in v["metadata"]["aliases"]
    assert 14328 in v["source_inbox_ids"]
    assert _row(otra)["status"] == "pendiente"
    # la marca se levantó: una segunda pasada no revisa nada
    assert asyncio.run(extract_mod.recheck_duplicates(1)).revisadas == 0


def test_recheck_sin_acuerdo_del_juez_no_funde_y_levanta_la_marca() -> None:
    # el juez de test (conftest) nunca funde
    vieja, nueva, _ = _gemelas()
    st = asyncio.run(extract_mod.recheck_duplicates(1))
    assert st.revisadas == 1 and st.fundidas == 0
    assert _row(nueva)["status"] == "pendiente" and _row(vieja)["status"] == "pendiente"
    assert _row(nueva)["metadata"].get("dedup_pending") in (None, False)


def test_recheck_dry_run_y_por_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: _JuezQueFunde())
    vieja, nueva, _ = _gemelas()
    st = asyncio.run(extract_mod.recheck_duplicates(1, tarea_ids=[nueva], apply=False))
    assert st.fundidas == 0 and st.pares[0]["into"] == vieja
    assert _row(nueva)["status"] == "pendiente"
    assert _row(nueva)["metadata"]["dedup_pending"] is True  # dry-run no toca nada


# --------------------------------------------------------------------- CLI y API


def test_cli_merge_y_recheck(capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: _JuezQueFunde())
    vieja, nueva, _ = _gemelas()
    assert tareas_main(["recheck", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"#{nueva}" in out and f"es la misma que #{vieja}" in out and "(dry-run)" in out
    assert tareas_main(["merge", "--into", str(vieja), "--absorb", str(nueva)]) == 0
    assert f"tarea #{nueva} fundida en #{vieja}" in capsys.readouterr().out
    assert _row(nueva)["status"] == "cancelada"


def test_api_merge(client: Any) -> None:
    vieja, nueva, _ = _gemelas()
    r = client.post(f"/tareas/{vieja}/merge", json={"absorb_id": nueva})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == vieja and 14328 in r.json()["source_inbox_ids"]
    assert _row(nueva)["status"] == "cancelada"
    assert client.post(f"/tareas/{vieja}/merge", json={"absorb_id": vieja}).status_code == 422
    assert client.post("/tareas/999999/merge", json={"absorb_id": vieja}).status_code == 404
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_tareas WHERE metadata->>'merged_into' = :v"),
            {"v": str(vieja)},
        ).scalar_one()
    assert n == 1


def test_relink_fecha_lo_enganchado_antes_sin_fecha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tareas que ya tienen asunto (se engancharon antes de este arreglo) pero quedaron sin fecha de
    próxima sesión: `relink` se las pone con el horario del curso."""
    monkeypatch.setattr(extract_mod, "_message_day", lambda conn, ids: date(2026, 9, 16))
    monkeypatch.setattr(
        extract_mod, "_next_session", lambda conn, uid, aid, after: date(2026, 9, 17)
    )
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        t = tareas.add(
            c,
            1,
            title="Traer el trabajo impreso",
            asunto=aid,
            origin="extraction",
            source_inbox_ids=[14328],
            metadata={"due_kind": "proxima_sesion"},
        )
        st = tareas.relink_orphans(c, 1, apply=False)
        assert st["sin_fecha"] == 1 and st["dated"] == 1
    assert _row(int(t["id"]))["due_on"] is None  # dry-run
    with connection() as c:
        tareas.relink_orphans(c, 1, apply=True)
    r = _row(int(t["id"]))
    assert r["due_on"] == "2026-09-17" and r["metadata"]["due_source"] == "proxima_sesion"


def test_candidatas_sin_la_propia_y_la_misma_sesion_primero() -> None:
    """La propia tarea no ocupa un lugar del tope, y la pedida en la misma sesión va antes que una
    vieja más parecida."""
    aid = asunto("Intensivo de inglés", kind="programa")
    with connection() as c:
        vieja = tareas.add(
            c,
            1,
            title="Ver el video y tomar notas",
            asunto=aid,
            origin="extraction",
            source_inbox_ids=[100],
            metadata={"due_fixed_day": "2026-09-01"},
        )
        gemela = tareas.add(
            c,
            1,
            title="Ver el video sobre present perfect continuous",
            asunto=aid,
            origin="extraction",
            source_inbox_ids=[14319],
            metadata={"due_fixed_day": "2026-09-16"},
        )
        nueva = tareas.add(
            c,
            1,
            title="Ver el video y responder las preguntas asignadas",
            asunto=aid,
            origin="extraction",
            source_inbox_ids=[14328],
            metadata={"due_fixed_day": "2026-09-16"},
        )
        cands = tareas.find_similar_candidates(
            c,
            1,
            title="Ver el video y responder las preguntas asignadas",
            asunto_id=aid,
            due_on=None,
            source_inbox_ids=[14328],
            fixed_day=date(2026, 9, 16),
            exclude_ids=[int(nueva["id"])],
        )
    ids = [int(x.tarea["id"]) for x in cands]
    assert int(nueva["id"]) not in ids
    assert ids[0] == int(gemela["id"]) and cands[0].same_session
    assert int(vieja["id"]) in ids


# --------------------------------------------------------------------- de punta a punta


@pytest.mark.asyncio
async def test_el_asignador_engancha_fecha_y_funde_solo(monkeypatch: pytest.MonkeyPatch) -> None:
    """El camino real: una nota sin asunto deja una tarea suelta; el asignador cuelga la nota del
    curso → la tarea se engancha, recibe fecha y el re-chequeo (con el juez) la funde en la que
    ya existía. Sin red: LLM del asignador y juez de mentira."""
    from memex.modules.asuntos.assign import run_assign
    from memex.modules.asuntos.settings import AsuntosSettings
    from tests.asuntos._seed import FakeLLM, msg, source

    monkeypatch.setattr(extract_mod, "_judge_client", lambda _uid: _JuezQueFunde())
    monkeypatch.setattr(extract_mod, "_message_day", lambda conn, ids: date(2026, 9, 16))
    monkeypatch.setattr(
        extract_mod, "_next_session", lambda conn, uid, aid, after: date(2026, 9, 17)
    )
    aid = asunto("Intensivo de inglés", kind="programa")
    src = source()
    nota = msg(src, subject="Notas de la clase", from_addr="notas@notion.local")
    with connection() as c:
        vieja = tareas.add(
            c,
            1,
            title="Ver el video sobre present perfect continuous",
            asunto=aid,
            due_on=date(2026, 9, 17),
            origin="extraction",
            source_inbox_ids=[14319],
        )
        suelta = tareas.add(
            c,
            1,
            title="Ver el video y responder las preguntas asignadas",
            origin="extraction",
            source_inbox_ids=[nota],
            metadata={"due_kind": "proxima_sesion"},
        )
    llm = FakeLLM(
        json.dumps(
            {
                "asignaciones": [
                    {"inbox_id": nota, "asunto": f"A{aid}", "confianza": 0.95, "hito": None}
                ],
                "nuevos": [],
                "actualizaciones": [],
                "relaciones": [],
            }
        )
    )
    res = await run_assign(1, [nota], client=llm, settings=AsuntosSettings())
    assert res.assigned == 1
    s = _row(int(suelta["id"]))
    assert s["asunto_id"] == aid
    assert s["status"] == "cancelada" and s["metadata"]["merged_into"] == int(vieja["id"])
    assert s["metadata"].get("dedup_pending") in (None, False)
    v = _row(int(vieja["id"]))
    assert v["status"] == "pendiente" and nota in v["source_inbox_ids"]
    assert "Ver el video y responder las preguntas asignadas" in v["metadata"]["aliases"]
