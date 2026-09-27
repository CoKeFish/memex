"""Parsers compartidos para respuestas de APIs OpenAI-compatible (`/chat/completions`).

DeepSeek (texto) y el proveedor de visión de OCR (`memex.ocr`) hablan el mismo dialecto:
`choices[0].message.content` + un objeto `usage` con la misma forma. Parsean idéntico, así que
las funciones viven acá para no duplicarlas (y que no diverjan).

Defensivo ante faltantes/shapes raros: un proveedor que devuelva algo inesperado produce
strings vacíos / ceros en vez de reventar el run.
"""

from __future__ import annotations

from typing import Any

from memex.llm.client import LLMUsage


def parse_choice(data: Any) -> tuple[str, str | None]:
    """Extrae `(content, finish_reason)` de `choices[0]` defensivamente."""
    if not isinstance(data, dict):
        return "", None
    choices = data.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return "", None
    first: dict[str, Any] = choices[0]
    message = first.get("message")
    content = str(message.get("content") or "") if isinstance(message, dict) else ""
    raw_reason = first.get("finish_reason")
    finish_reason = raw_reason if isinstance(raw_reason, str) else None
    return content, finish_reason


def parse_usage(raw: Any) -> LLMUsage:
    """Mapea el objeto `usage` (forma OpenAI/DeepSeek) a `LLMUsage` (defensivo)."""
    u: dict[str, Any] = raw if isinstance(raw, dict) else {}
    prompt = as_int(u.get("prompt_tokens"))
    completion = as_int(u.get("completion_tokens"))
    total = as_int(u.get("total_tokens")) or (prompt + completion)
    hit = as_int(u.get("prompt_cache_hit_tokens"))
    miss_raw = u.get("prompt_cache_miss_tokens")
    miss = as_int(miss_raw) if miss_raw is not None else max(prompt - hit, 0)
    details = u.get("completion_tokens_details")
    reasoning = as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        reasoning_tokens=reasoning,
    )


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
