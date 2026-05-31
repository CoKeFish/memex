"""Cross-check (a demanda) de nuestra tabla de pricing vs el registro de tokencost.

tokencost NO es dependencia del proyecto: el runtime usa SIEMPRE nuestra tabla
(`pricing.py`), que para DeepSeek es más fresca/precisa que los registros comunitarios
(a 2026-05-31 tokencost tenía el precio viejo de deepseek-chat). Este test queda para
"mantener ambos y evaluar el drift por un tiempo": se corre a demanda con tokencost
efímero y reporta la comparación —

    uv run --with tokencost pytest tests/llm/test_pricing_vs_tokencost.py -s

`importorskip` lo saltea en la suite normal (sin tokencost instalado), así no agrega
dependencia ni rompe CI. No exige igualdad (tokencost suele ir desfasado para DeepSeek):
solo chequea que nuestro `output` esté dentro de un orden de magnitud del de tokencost
cuando ambos existen —atrapa un typo de decimal, tolera el drift conocido (~2-3x).
"""

from __future__ import annotations

from typing import Any

import pytest

from memex.llm.pricing import MODEL_PRICING

TOKEN_COSTS: dict[str, Any] = pytest.importorskip("tokencost.constants").TOKEN_COSTS


def _per_million(key: str) -> dict[str, float | None] | None:
    """Precios de tokencost (por 1M de tokens) para `key`, o None si no está."""
    costs = TOKEN_COSTS.get(key)
    if not costs:
        return None
    miss = costs.get("input_cost_per_token")
    hit = costs.get("input_cost_per_token_cache_hit") or costs.get("cache_read_input_token_cost")
    out = costs.get("output_cost_per_token")
    scale = 1_000_000
    return {
        "cache_hit": float(hit) * scale if hit is not None else None,
        "cache_miss": float(miss) * scale if miss is not None else None,
        "output": float(out) * scale if out is not None else None,
    }


def test_report_pricing_vs_tokencost() -> None:
    print("\n=== memex MODEL_PRICING vs tokencost (USD / 1M tokens) ===")
    for model, ours in MODEL_PRICING.items():
        theirs = _per_million(model) or _per_million(f"deepseek/{model}")
        print(f"\n{model}")
        print(f"  ours     : hit={ours.cache_hit} miss={ours.cache_miss} out={ours.output}")
        print(f"  tokencost: {theirs}")
        their_out = theirs["output"] if theirs else None
        if their_out:
            ratio = float(ours.output) / their_out
            assert 0.1 <= ratio <= 10, (
                f"{model}: output {ours.output} vs tokencost {their_out} (ratio {ratio:.2f}) "
                "— fuera de un orden de magnitud, revisar la tabla"
            )
