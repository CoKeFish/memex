"""Categoría de la tarea (2026-09-26): vocabulario fijo, alta/edición/filtro por CLI y módulo, las
de seguridad primero en la lista; en la extracción un aviso de seguridad sin fecha vale
`SEGURIDAD_DIAS` y una tarea con puntos es `dinamica` aunque el modelo diga otra cosa."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from memex.db import connection
from memex.modules.tareas import module as tareas
from memex.modules.tareas.extract import SEGURIDAD_DIAS
from memex.modules.tareas.module import CATEGORIES, category_value, list_tareas
from memex.modules.tareas.prompt import TAREAS_SYSTEM_PROMPT
from memex.modules.tareas.schema import TareaItem
from tests.tareas.test_extract import _asunto, _chat_inbox, _persist, _rows, _source


def test_category_value_normaliza_y_valida() -> None:
    assert category_value(None) == "otro" and category_value("") == "otro"
    assert category_value("Seguridad") == "seguridad"
    assert set(CATEGORIES) == {
        "seguridad",
        "entrega",
        "tramite",
        "pago",
        "inscripcion",
        "dinamica",
        "otro",
    }
    with pytest.raises(ValueError):
        category_value("urgente")


def test_add_update_list_con_categoria() -> None:
    curso = _asunto(f"Curso {uuid4().hex[:5]}")
    with connection() as c:
        a = tareas.add(c, 1, title="Pagar la matrícula", asunto=curso, category="pago")
        b = tareas.add(c, 1, title="Entregar el ensayo", asunto=curso, due_on=date(2026, 9, 2))
        s = tareas.add(
            c,
            1,
            title="Verificar el intento de acceso",
            asunto=curso,
            due_on=date(2099, 1, 1),
            category="seguridad",
        )
        assert a["category"] == "pago" and b["category"] == "otro"
        tareas.update(c, 1, b["id"], category="entrega")
        assert tareas.get(c, 1, b["id"])["category"] == "entrega"
        with pytest.raises(ValueError):
            tareas.update(c, 1, b["id"], category="rara")
        # filtro
        assert [t["id"] for t in list_tareas(c, 1, asunto_id=curso, category="pago")] == [a["id"]]
        # orden: seguridad primero aunque su fecha sea la más lejana; después por fecha
        ids = [t["id"] for t in list_tareas(c, 1, asunto_id=curso)]
        assert ids[0] == s["id"] and ids[1] == b["id"]


def test_extraccion_seguridad_sin_fecha_vale_dos_dias_y_puntos_es_dinamica() -> None:
    src = _source("whatsapp", mode="programa")
    chat = f"{uuid4().hex[:12]}@g.us"
    m = _chat_inbox(src, chat, when="2026-09-19 12:15:00+00")
    n = _chat_inbox(src, chat, when="2026-09-19 12:20:00+00")
    _persist(
        (m, n),
        [
            TareaItem(
                source_inbox_ids=(m,),
                title="Verificar el intento de acceso a la tienda",
                category="seguridad",
                priority="alta",
            ),
            TareaItem(
                source_inbox_ids=(n,),
                title="Publicar la foto del reto",
                category="entrega",
                points=40,
            ),
        ],
    )
    (seg,) = _rows(m)
    assert seg["due_on"] == date(2026, 9, 19 + SEGURIDAD_DIAS)
    with connection() as c:
        full = tareas.get(c, 1, int(seg["id"]))
    assert full["category"] == "seguridad" and full["metadata"]["due_source"] == "seguridad"
    (din,) = _rows(n)
    with connection() as c:
        assert tareas.get(c, 1, int(din["id"]))["category"] == "dinamica"


def test_el_prompt_pide_la_categoria_y_captura_seguridad() -> None:
    assert "`category`" in TAREAS_SYSTEM_PROMPT
    assert "EXCEPCIÓN: un aviso de SEGURIDAD" in TAREAS_SYSTEM_PROMPT
    assert "'seguridad'" in TAREAS_SYSTEM_PROMPT
    # el plazo que el aviso SÍ da («dentro de 30 días») se conserva; el corto es solo sin plazo
    assert "si el aviso da un plazo" in TAREAS_SYSTEM_PROMPT


def test_el_enrutador_sabe_que_los_avisos_de_seguridad_son_de_tareas() -> None:
    """El router elige módulos por `interest`: sin esto el aviso de Amazon iba solo a identidades
    y el extractor de tareas nunca lo veía (2026-09-26)."""
    from memex.modules.tareas.extract import TareasModule

    assert "AVISOS DE SEGURIDAD" in TareasModule.interest
