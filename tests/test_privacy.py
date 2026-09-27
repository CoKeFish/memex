"""Lista externa de datos personales (`memex.privacy`) y la guarda `scripts/check_personal_data.py`:
palabra completa sin distinguir mayúsculas, regex con `re:`, comentarios, y sin archivo → vacía."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from memex.privacy import find_hits, load_denylist, parse_denylist

LISTA = """
# comentario
Fulanito Pérez
fulanito@example.com
-1001234567890
re:\\b57300\\d{7}\\b
"""


def test_parse_y_hits() -> None:
    rules = parse_denylist(LISTA)
    assert [r.term for r in rules] == [
        "Fulanito Pérez",
        "fulanito@example.com",
        "-1001234567890",
        "re:\\b57300\\d{7}\\b",
    ]
    text = (
        "hola FULANITO PÉREZ\nmail: fulanito@example.com\nchat -1001234567890\nfono 573001234567\n"
    )
    assert find_hits(text, rules) == [
        (1, "Fulanito Pérez"),
        (2, "fulanito@example.com"),
        (3, "-1001234567890"),
        (4, "re:\\b57300\\d{7}\\b"),
    ]
    # palabra completa: «Fulanito Pérezuela» no es «Fulanito Pérez»
    assert find_hits("Fulanito Pérezuela", rules) == []


def test_sin_archivo_lista_vacia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMEX_PERSONAL_DENYLIST", str(tmp_path / "no-existe.txt"))
    assert load_denylist() == []


def test_script_falla_con_hallazgos_y_pasa_sin_lista(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_personal_data.py"
    lista = tmp_path / "lista.txt"
    lista.write_text("Fulanito Pérez\n", encoding="utf-8")
    sucio = tmp_path / "sucio.md"
    sucio.write_text("Nota sobre Fulanito Pérez.\n", encoding="utf-8")
    limpio = tmp_path / "limpio.md"
    limpio.write_text("Nota sobre una persona.\n", encoding="utf-8")
    env = {"MEMEX_PERSONAL_DENYLIST": str(lista)}
    r = subprocess.run(
        [sys.executable, str(script), str(sucio), str(limpio)],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **env},
    )
    assert r.returncode == 1 and "sucio.md:1: Fulanito Pérez" in r.stderr
    r = subprocess.run(
        [sys.executable, str(script), str(limpio)],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **env},
    )
    assert r.returncode == 0
    r = subprocess.run(
        [sys.executable, str(script), str(sucio)],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "MEMEX_PERSONAL_DENYLIST": str(tmp_path / "nada.txt")},
    )
    assert r.returncode == 0 and "sin lista" in r.stderr
