"""Tolerancia de los parsers a salida estilo codex (JSON entre fences ```json``` / prosa).

VERIFICACIÓN del experimento de codex (NO corre LLM): codex/anthropic devuelven JSON solo por
prompt y a veces lo envuelven en fences/prosa. El saneo vive EN EL CLIENTE
(`normalize_json_output`, activado por `response_format="json_object"` en CodexClient y
AnthropicClient): los parsers de los workers reciben JSON limpio. Los parsers en sí siguen
haciendo `json.loads` directo y ante fences DEGRADAN a su fallback seguro — segunda línea de
defensa, lockeada acá. Ver docs/experimento-codex-apartados.md.
"""

from __future__ import annotations

from memex.llm._json import normalize_json_output
from memex.modules.identidades.dedup_llm import _parse_decision
from memex.modules.routing import parse_routing
from memex.relevance.prompts import _strip_fences

_BARE = '{"modules": ["finance", "calendar"]}'
_FENCED = "```json\n" + _BARE + "\n```"


def test_routing_parses_bare_json() -> None:
    assert parse_routing(_BARE) == ["finance", "calendar"]


def test_routing_degrades_safely_on_fences() -> None:
    # fences → json.loads falla → None → el caller cae a "todos los candidatos" (seguro, no crash)
    assert parse_routing(_FENCED) is None


def test_dedup_parses_bare_json() -> None:
    d = _parse_decision('{"same": true, "confidence": 0.9, "rationale": "ok"}')
    assert d.same is True and d.confidence == 0.9


def test_dedup_degrades_safely_on_fences() -> None:
    # fences → json.loads falla → fallback seguro (sesgo a coexistir: NO fusiona)
    d = _parse_decision('```json\n{"same": true, "confidence": 0.9}\n```')
    assert d.same is False and d.rationale == "parse_fallback"


def test_gate_is_the_one_that_strips_fences() -> None:
    # El helper propio del gate (anterior al saneo en el cliente) sigue funcionando como
    # defensa redundante e inofensiva sobre JSON ya limpio.
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('{"a": 1}') == '{"a": 1}'


def test_client_normalization_closes_the_gap_end_to_end() -> None:
    # El flujo real: el cliente sanea (response_format=json_object) → el parser del worker
    # recibe JSON limpio y SÍ parsea, en vez de degradar.
    assert parse_routing(normalize_json_output(_FENCED)) == ["finance", "calendar"]
    d = _parse_decision(normalize_json_output('```json\n{"same": true, "confidence": 0.9}\n```'))
    assert d.same is True and d.confidence == 0.9
