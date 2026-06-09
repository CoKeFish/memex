"""compute_ocr_cost — costo por modelo de OCR (visión). El input incluye los tokens de imagen."""

from __future__ import annotations

from decimal import Decimal

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
