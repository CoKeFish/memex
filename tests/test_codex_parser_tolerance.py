"""Tolerancia de los parsers a salida estilo codex (JSON entre fences ```json``` / prosa).

VERIFICACIÓN del experimento de codex (NO corre LLM): codex devuelve JSON solo por prompt y a
veces lo envuelve en fences. Solo el gate de relevancia strip-ea fences (por eso codex corre
30/30 ahí); los demás consumidores JSON (routing, dedup, ...) hacen `json.loads` directo → ante
fences DEGRADAN a un fallback seguro (no crashean), no parsean. El summarizer es texto plano →
tolerante total. Estos tests lockean ese comportamiento por-consumidor; ver
docs/experimento-codex-apartados.md para el veredicto y la recomendación.
"""

from __future__ import annotations

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
    # Contraste: el helper del gate SÍ tolera fences — el patrón a replicar si se quiere
    # robustez (no solo degradación) en los demás consumidores con codex.
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('{"a": 1}') == '{"a": 1}'
