"""Capa LLM provider-agnóstica de memex.

API pública: tipá tus callers contra el Protocol `LLMClient` y construí el concreto
(`DeepSeekClient`) en el borde. `LLMResult.cost_usd` (Decimal) y `LLMResult.usage`
mapean directo a `memex.core.observability.record_llm_call`.

Uso típico (async):

    from memex.llm import DeepSeekClient, LLMConfig, ChatMessage

    client = DeepSeekClient(LLMConfig.from_env())
    result = await client.complete([ChatMessage("user", "hola")])
    # result.content, result.cost_usd, result.usage, result.latency_ms

Construcción pluggable (`registry.py`):

- **Modelo por llamada**: ya soportado — `complete(..., model="deepseek-v4-pro")`.
- **Proveedor swappable**: cada proveedor es una clase contra el Protocol `LLMClient`
  (`DeepSeekClient`, `AnthropicClient`, `CodexClient`) — los callers no se enteran.
- **Punto único por consumidor**: `build_llm_client(consumer)` resuelve provider+model de
  `llm_consumer_settings` (config en runtime, sin tocar código). `aclose_llm(client)` cierra
  el cliente sin conocer su tipo concreto.
- **Cadena de fallback**: `FallbackClient` envuelve varios proveedores tras el mismo Protocol
  y salta de uno a otro ante cuota/red/timeout.
"""

from memex.llm.anthropic import AnthropicClient, AnthropicError, anthropic_config
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
from memex.llm.codex import CodexClient, CodexError
from memex.llm.config import LLMConfig, LLMConfigError
from memex.llm.deepseek import DeepSeekClient, DeepSeekError
from memex.llm.fallback import FallbackClient
from memex.llm.pricing import (
    MODEL_PRICING,
    ModelPricing,
    PricingConfigError,
    compute_cost,
    is_off_peak,
    load_pricing,
)
from memex.llm.registry import aclose_llm, build_llm_client

__all__ = [
    "MODEL_PRICING",
    "AnthropicClient",
    "AnthropicError",
    "ChatMessage",
    "CodexClient",
    "CodexError",
    "DeepSeekClient",
    "DeepSeekError",
    "FallbackClient",
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
    "aclose_llm",
    "anthropic_config",
    "build_llm_client",
    "compute_cost",
    "is_off_peak",
    "load_pricing",
]
