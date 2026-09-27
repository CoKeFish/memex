"""Normalizador de salida JSON-por-prompt (codex/anthropic): fences y prosa alrededor.

Los proveedores SIN modo JSON nativo (`CodexClient`, `AnthropicClient`) piden el JSON por
prompt, y los modelos a veces lo envuelven en fences ```json``` o prosa («Aquí está: ...»).
Los parsers de los callers hacen `json.loads` directo y ante eso DEGRADAN a su fallback
seguro (routing→todos, dedup→no-merge) — no crashean, pero tampoco parsean. Este helper
cierra ese hueco EN EL CLIENTE (donde se encapsulan las rarezas de cada vendor, como el
`stop_reason` de Anthropic): cuando el caller pidió `response_format="json_object"`, el
cliente normaliza el contenido antes de devolverlo.

Regla conservadora: SOLO se sustituye el contenido cuando el candidato extraído ES JSON
válido; si nada parsea, se devuelve el original intacto y el parser del caller decide
(degrada seguro, como hoy). Sobre el JSON desnudo de DeepSeek/modo nativo es un no-op.
DeepSeek no lo usa (modo JSON forzado por API).
"""

from __future__ import annotations

import json


def _parses(candidate: str) -> bool:
    try:
        json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _outermost_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    """El slice entre la primera apertura y el último cierre, si es JSON válido."""
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    return candidate if _parses(candidate) else None


def normalize_json_output(content: str) -> str:
    """Extrae el JSON (objeto o lista) de una salida con fences/prosa alrededor.

    Fast path: si el contenido ya parsea, vuelve tal cual (strip de whitespace). Si no, se
    prueba el slice más externo `{...}` y luego `[...]` — `json.loads` valida, así que un
    cierre de prosa engañoso simplemente descarta el candidato. Nada parsea → el original.
    """
    cleaned = content.strip()
    if _parses(cleaned):
        return cleaned
    # El delimitador que ABRE primero manda: en un array fenced, el slice {…} robaría el
    # primer objeto interior si se probara antes que [...].
    pairs = sorted(
        (("{", "}"), ("[", "]")),
        key=lambda p: pos if (pos := cleaned.find(p[0])) != -1 else len(cleaned),
    )
    for open_ch, close_ch in pairs:
        candidate = _outermost_slice(cleaned, open_ch, close_ch)
        if candidate is not None:
            return candidate
    return content
