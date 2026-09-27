"""normalize_json_output: extrae JSON de fences/prosa SOLO si parsea; si no, original intacto."""

from __future__ import annotations

import pytest

from memex.llm._json import normalize_json_output

_OBJ = '{"modules": ["finance"], "n": 1}'
_ARR = '[{"id": 7, "verdict": "relevant"}]'


def test_bare_json_passes_through() -> None:
    assert normalize_json_output(_OBJ) == _OBJ
    assert normalize_json_output(_ARR) == _ARR


def test_whitespace_is_stripped() -> None:
    assert normalize_json_output(f"\n  {_OBJ}  \n") == _OBJ


@pytest.mark.parametrize(
    "wrapped",
    [
        f"```json\n{_OBJ}\n```",
        f"```\n{_OBJ}\n```",
        f"Aquí está el resultado:\n```json\n{_OBJ}\n```\nEspero que sirva.",
        f"El JSON pedido: {_OBJ}",
    ],
)
def test_extracts_object_from_fences_and_prose(wrapped: str) -> None:
    assert normalize_json_output(wrapped) == _OBJ


def test_extracts_array_from_fences() -> None:
    assert normalize_json_output(f"```json\n{_ARR}\n```") == _ARR


def test_invalid_json_returns_original_untouched() -> None:
    raw = "```json\n{esto no es json}\n```"
    assert normalize_json_output(raw) == raw  # el parser del caller degrada, como hoy


def test_prose_without_json_returns_original() -> None:
    raw = "No pude generar el JSON pedido."
    assert normalize_json_output(raw) == raw


def test_misleading_braces_in_prose_do_not_break_extraction() -> None:
    # el slice primer-{ / último-} no parsea (incluye prosa) → se devuelve el original
    raw = "uso {llaves} sueltas y al final {otra}"
    assert normalize_json_output(raw) == raw
