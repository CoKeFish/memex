"""Los tres PRINCIPIOS GENERALES (`modules/principios.py`) viven en UN lugar y entran tal cual en
cada prompt de sistema que registra cosas de la persona (tareas, calendario y su juez de estado,
identidades, asuntos); cada módulo agrega su lectura («por el principio N…»); y ninguna regla
nombra marcas ni casos del dueño (regla «prompts sin marcas»: describen categorías)."""

from __future__ import annotations

import re

import pytest

from memex.modules.asuntos.assign import SYSTEM_PROMPT as ASUNTOS_SYSTEM_PROMPT
from memex.modules.calendar.commitment import JUDGE_SYSTEM_PROMPT
from memex.modules.calendar.prompt import CALENDAR_SYSTEM_PROMPT
from memex.modules.identidades.prompt import IDENTIDADES_SYSTEM_PROMPT
from memex.modules.principios import PRINCIPIOS_GENERALES
from memex.modules.tareas.prompt import TAREAS_SYSTEM_PROMPT

PROMPTS: dict[str, str] = {
    "tareas": TAREAS_SYSTEM_PROMPT,
    "calendar": CALENDAR_SYSTEM_PROMPT,
    "calendar_judge": JUDGE_SYSTEM_PROMPT,
    "identidades": IDENTIDADES_SYSTEM_PROMPT,
    "asuntos": ASUNTOS_SYSTEM_PROMPT,
}

#: Marcas, programas y casos puntuales que motivaron reglas: NUNCA en una regla. Los genéricos
#: van acá; los del dueño (nombres, organizaciones de su vida) viven en la lista EXTERNA
#: (`memex.privacy`, fuera del repo) y se suman si existe en esta máquina.
_MARCAS_GENERICAS = ("uber", "autodesk", "harold")


def _marcas() -> list[str]:
    from memex.privacy import load_denylist

    externas = [r.term.lower() for r in load_denylist() if not r.term.startswith("re:")]
    return sorted({*_MARCAS_GENERICAS, *externas})


def test_los_tres_principios_estan_enunciados() -> None:
    for titulo in (
        "PROPIO O PARA TODOS",
        "UNA COSA DEL MUNDO, UN REGISTRO",
        "LA SEÑAL PERTENECE AL HILO",
    ):
        assert titulo in PRINCIPIOS_GENERALES


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_cada_prompt_incluye_los_principios_tal_cual(name: str) -> None:
    assert PRINCIPIOS_GENERALES in PROMPTS[name]


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_las_reglas_no_nombran_marcas_ni_casos(name: str) -> None:
    bajo = PROMPTS[name].lower()
    assert [m for m in _marcas() if re.search(rf"(?<!\w){re.escape(m)}(?!\w)", bajo)] == []


def test_cada_modulo_aplica_su_lectura() -> None:
    # 1: exigencia vs. llamado masivo / condicional / ánimo (tareas)
    assert "EXIGENCIA" in TAREAS_SYSTEM_PROMPT and "CONDICIONAL" in TAREAS_SYSTEM_PROMPT
    assert "es ánimo, no tarea" in TAREAS_SYSTEM_PROMPT
    # 2: una cosa = un evento; 1+3: el estado se juzga por la propia persona (calendario y juez)
    assert "UNA cosa del mundo es UN evento" in CALENDAR_SYSTEM_PROMPT
    assert "OTRA persona nunca suben el\n  estado" in CALENDAR_SYSTEM_PROMPT
    assert "PROPIA persona" in JUDGE_SYSTEM_PROMPT
    # 1: vínculo directo, lo demás es mención; 2: el remitente es una sola identidad
    assert "NO tiene vínculo: es `mencion`" in IDENTIDADES_SYSTEM_PROMPT
    assert "el REMITENTE es UNA identidad" in IDENTIDADES_SYSTEM_PROMPT
    # 1 y 3 en asuntos: ánimo no es estado; el hilo manda
    assert "ÁNIMO NO ES ESTADO" in ASUNTOS_SYSTEM_PROMPT
    assert "EL HILO MANDA" in ASUNTOS_SYSTEM_PROMPT
