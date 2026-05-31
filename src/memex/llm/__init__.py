"""Capa LLM provider-agnóstica de memex.

API pública: tipá tus callers contra el Protocol `LLMClient` y construí el concreto
(`DeepSeekClient`) en el borde. `LLMResult.cost_usd` (Decimal) y `LLMResult.usage`
mapean directo a `memex.core.observability.record_llm_call`.

Uso típico (async):

    from memex.llm import DeepSeekClient, LLMConfig, ChatMessage

    client = DeepSeekClient(LLMConfig.from_env())
    result = await client.complete([ChatMessage("user", "hola")])
    # result.content, result.cost_usd, result.usage, result.latency_ms

Diseño futuro (DOCUMENTADO, no implementado acá):

- **Modelo por llamada**: ya soportado — `complete(..., model="deepseek-v4-pro")`.
- **Proveedor swappable**: implementar otra clase contra el Protocol `LLMClient`
  (p. ej. `OpenAIClient`) sin tocar a los callers.
- **Selección por categoría de ingestor o por mensaje concreto**: un futuro
  registry/factory podría elegir proveedor+modelo según el tier del classifier
  (ADR-002) — p. ej. `blacklist`→sin LLM, `batch`→flash, `individual`→pro — o
  incluso por mensaje. Esa lógica de selección NO vive acá; este paquete solo
  provee el primitivo agnóstico sobre el que se construirá.
"""

from memex.llm.client import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMQuotaError,
    LLMResult,
    LLMUsage,
    ResponseFormat,
    Role,
)
from memex.llm.config import LLMConfig, LLMConfigError
from memex.llm.deepseek import DeepSeekClient, DeepSeekError
from memex.llm.pricing import (
    MODEL_PRICING,
    ModelPricing,
    PricingConfigError,
    compute_cost,
    is_off_peak,
    load_pricing,
)

__all__ = [
    "MODEL_PRICING",
    "ChatMessage",
    "DeepSeekClient",
    "DeepSeekError",
    "LLMClient",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "LLMQuotaError",
    "LLMResult",
    "LLMUsage",
    "ModelPricing",
    "PricingConfigError",
    "ResponseFormat",
    "Role",
    "compute_cost",
    "is_off_peak",
    "load_pricing",
]
