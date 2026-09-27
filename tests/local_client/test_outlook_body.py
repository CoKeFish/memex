"""Cuerpo del plugin outlook-desktop: `item.Body` MÁS el texto de las imágenes del `HTMLBody`.

Caso real (FIB, 2026-09-14): el cuerpo de texto decía «Te invitamos a nuestro bootcamp virtual» y
nada más; el nombre del bootcamp, las fechas y el botón de postulación estaban en tres PNG remotos.
El correo se marcó relevante y la extracción sacó cero eventos y cero tareas.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "outlook-desktop" / "__init__.py"


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("outlook_desktop_body_test", PLUGIN)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


class _Item:
    def __init__(self, body: str, html: str = "") -> None:
        self.Body = body
        self.HTMLBody = html


def test_el_texto_de_las_imagenes_se_suma_al_cuerpo(mod: ModuleType) -> None:
    html = (
        "<html><body>"
        '<img src="h.png" alt="Logo">'
        '<img src="a.png" alt="Bootcamp Web3 Esenciales, 22 y 23 de septiembre">'
        '<img src="b.png" alt="Postulate aqui: cierre 20 de septiembre">'
        "</body></html>"
    )
    item = _Item("Te invitamos a nuestro bootcamp virtual", html)
    body, truncado = mod._body_text(item, 500_000)
    assert truncado is False
    assert "Te invitamos a nuestro bootcamp virtual" in body
    assert "Bootcamp Web3 Esenciales, 22 y 23 de septiembre" in body
    assert "Postulate aqui: cierre 20 de septiembre" in body
    assert "Logo" not in body


def test_lo_que_ya_estaba_en_el_cuerpo_no_se_repite(mod: ModuleType) -> None:
    html = '<html><body><img src="a.png" alt="Charla del 3 de octubre"></body></html>'
    item = _Item("Charla del 3 de octubre, inscribite antes del 28", html)
    body, _ = mod._body_text(item, 500_000)
    assert body.count("Charla del 3 de octubre") == 1


def test_sin_html_el_cuerpo_queda_igual(mod: ModuleType) -> None:
    item = _Item("Tu recibo de Uber: total COP 24.139.")
    assert mod._body_text(item, 500_000)[0] == "Tu recibo de Uber: total COP 24.139."


def test_un_htmlbody_roto_no_tumba_la_ingesta(mod: ModuleType) -> None:
    """El COM de Outlook devuelve cosas raras; perder el correo entero por eso sería peor."""

    class _Roto:
        Body = "cuerpo que si sirve"

        @property
        def HTMLBody(self) -> Any:
            raise RuntimeError("COM dijo que no")

    assert mod._body_text(_Roto(), 500_000)[0] == "cuerpo que si sirve"
