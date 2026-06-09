"""Tabla de precios + cálculo de costo por llamada de OCR (USD).

Como `memex.llm.pricing`: el proveedor devuelve conteos de tokens, no costo; lo calculamos acá
desde una tabla por modelo. Para visión, el costo de la imagen viene FOLDED dentro de
`prompt_tokens` por los proveedores OpenAI-compatible, así que alcanza con input/output por 1M.

⚠ PRECIOS VOLÁTILES — la tabla arranca VACÍA a propósito: modelo no tabulado → `Decimal(0)`
(no revienta el run; el cliente loguea el modelo para detectar que falta tabular). Agregá el
modelo que uses con su precio verificado del proveedor.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from memex.llm.client import LLMUsage

_PER_MILLION = Decimal(1_000_000)
#: Precisión de la columna llm_calls.cost_usd (NUMERIC(10,6)).
_COST_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class OcrPricing:
    """Precio de un modelo de visión en USD por 1M de tokens (input incluye los de imagen)."""

    input: Decimal
    output: Decimal


#: Sembrá acá el/los modelos que uses, con precio verificado del proveedor (USD por 1M tokens).
#: gpt-4o-mini = tarifa estándar OpenAI (input 0.15 / output 0.60); es el modelo OCR default. Para
#: otro proveedor/modelo (p. ej. un vision open-source), sumá su fila con el precio verificado.
MODEL_PRICING: dict[str, OcrPricing] = {
    "gpt-4o-mini": OcrPricing(Decimal("0.15"), Decimal("0.60")),
}


def compute_ocr_cost(model: str, usage: LLMUsage) -> Decimal:
    """Costo USD de una llamada de OCR, cuantizado a 6 decimales.

    Modelo no tabulado → `Decimal(0)`: no revienta el run (igual que `llm.pricing.compute_cost`).
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return Decimal(0)
    cost = (
        pricing.input * usage.prompt_tokens + pricing.output * usage.completion_tokens
    ) / _PER_MILLION
    return cost.quantize(_COST_QUANTUM)
