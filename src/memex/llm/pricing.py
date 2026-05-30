"""Tabla de precios + cálculo de costo por llamada (USD).

DeepSeek devuelve solo conteos de tokens (no el costo en USD, a diferencia de
Apify) → el costo se calcula acá desde una tabla por modelo, distinguiendo tokens
de prompt servidos desde cache (`cache_hit`, más baratos) de los no-cacheados
(`cache_miss`) y los de salida (`output`).

⚠ PRECIOS VOLÁTILES — verificados 2026-05-29 desde
https://api-docs.deepseek.com/quick_start/pricing. DeepSeek los cambia seguido
(había una promo -75% en v4-pro venciendo 2026-05-31 15:59 UTC). Si cambian,
actualizar `MODEL_PRICING` acá: es el único lugar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from memex.llm.client import LLMUsage

_PER_MILLION = Decimal(1_000_000)
#: Precisión de la columna llm_calls.cost_usd (NUMERIC(10,6)).
_COST_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPricing:
    """Precio de un modelo en USD por 1M de tokens."""

    cache_hit: Decimal
    cache_miss: Decimal
    output: Decimal


# Flash (barato) y Pro (capaz), más los alias legacy deepseek-chat / deepseek-reasoner
# (→ mapean a v4-flash y comparten su precio).
_FLASH = ModelPricing(Decimal("0.14"), Decimal("0.28"), Decimal("0.28"))
_PRO = ModelPricing(Decimal("0.435"), Decimal("1.74"), Decimal("3.48"))

MODEL_PRICING: dict[str, ModelPricing] = {
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-pro": _PRO,
    "deepseek-chat": _FLASH,
    "deepseek-reasoner": _FLASH,
}


def compute_cost(model: str, usage: LLMUsage) -> Decimal:
    """Costo USD de una llamada, cuantizado a 6 decimales.

    Modelo no tabulado → `Decimal(0)`: no revienta el run (el cliente loguea el
    modelo para que se detecte una tabla desactualizada).
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return Decimal(0)

    cost = (
        pricing.cache_hit * usage.cache_hit_tokens
        + pricing.cache_miss * usage.cache_miss_tokens
        + pricing.output * usage.completion_tokens
    ) / _PER_MILLION
    return cost.quantize(_COST_QUANTUM)
