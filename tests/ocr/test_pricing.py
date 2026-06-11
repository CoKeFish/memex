"""compute_ocr_cost — costo por modelo de OCR (visión). El input incluye los tokens de imagen."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from memex.llm.client import LLMUsage
from memex.ocr.pricing import compute_ocr_cost


def _usage(*, prompt: int = 0, completion: int = 0) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


def test_gpt_4o_mini_priced() -> None:
    # gpt-4o-mini = 0.15 input / 0.60 output por 1M (H-4: antes faltaba en la tabla → costo $0).
    # 1M prompt * 0.15 + 1M completion * 0.60 = 0.75
    cost = compute_ocr_cost("gpt-4o-mini", _usage(prompt=1_000_000, completion=1_000_000))
    assert cost == Decimal("0.750000")


def test_unknown_ocr_model_returns_zero() -> None:
    # Un modelo sin tabular (p. ej. el qwen de prueba) sigue devolviendo 0, sin reventar.
    assert compute_ocr_cost("qwen2.5-vl-7b", _usage(prompt=1000, completion=1000)) == Decimal(0)


def test_unknown_ocr_model_warns_once_per_model(sink_capture: Any, monkeypatch: Any) -> None:
    """Modelo de visión sin tabular → $0 con `ocr.pricing.unknown_model` UNA vez por modelo,
    espejo de `llm.pricing` (H-4: el $0 silencioso no puede ser el mecanismo de detección)."""
    from memex.ocr import pricing as pricing_mod

    monkeypatch.setattr(pricing_mod, "_WARNED_UNKNOWN", set())  # aislar del resto de la suite
    u = _usage(prompt=1000, completion=1000)
    assert compute_ocr_cost("vision-fantasma", u) == Decimal(0)
    assert compute_ocr_cost("vision-fantasma", u) == Decimal(0)

    records = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    warned = [r for r in records if r["event"] == "ocr.pricing.unknown_model"]
    assert len(warned) == 1
    assert warned[0]["level"] == "warning"
    assert json.loads(warned[0]["fields"])["model"] == "vision-fantasma"
