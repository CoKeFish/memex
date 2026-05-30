"""Contrato provider-agnóstico de la capa LLM.

Define el Protocol `LLMClient` (la abstracción contra la que tipan los callers) y
los tipos de datos que viajan por él (`ChatMessage`, `LLMUsage`, `LLMResult`). Un
proveedor concreto (p. ej. `DeepSeekClient`) implementa este Protocol; los callers
NUNCA tipan contra la clase concreta, igual que el runner tipa contra `MemexSink`
y no contra `MemexServerClient`.

`LLMResult` está pensado para mapear 1:1 a los argumentos de
`memex.core.observability.record_llm_call` (model, prompt_tokens, completion_tokens,
cost_usd, latency_ms) — ese es el *seam* con el futuro classifier/summarizer, que
queda fuera de alcance acá.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]

#: Formato de salida pedido al modelo. "json_object" activa el modo JSON del
#: proveedor (el caller DEBE pedir JSON también en el prompt — requisito de DeepSeek).
ResponseFormat = Literal["text", "json_object"]


@dataclass(frozen=True)
class ChatMessage:
    """Un turno de la conversación enviado al modelo."""

    role: Role
    content: str


@dataclass(frozen=True)
class LLMUsage:
    """Conteo de tokens de una llamada.

    `cache_hit_tokens` + `cache_miss_tokens` particionan `prompt_tokens` (los hits
    son tokens de prompt servidos desde el cache del proveedor, más baratos).
    `reasoning_tokens` solo aplica a modelos con modo thinking/reasoner.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    """Resultado de una completion: texto + usage + costo calculado + latencia."""

    content: str
    model: str
    usage: LLMUsage
    cost_usd: Decimal
    latency_ms: int
    finish_reason: str | None = None


class LLMError(Exception):
    """Base de todos los errores de la capa LLM — los callers la atrapan genérica.

    `status_code` es el HTTP status cuando aplica, o 0 para errores lógicos / de
    configuración. Mismo shape que `ApifyError` para mantener la convención.
    """

    def __init__(self, status_code: int, message: str, body: str | None = None) -> None:
        super().__init__(f"llm error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


@runtime_checkable
class LLMClient(Protocol):
    """Interfaz de chat-completion agnóstica del proveedor.

    Una implementación concreta aísla a su vendor (HTTP, auth, shapes) detrás de
    este único método. `model=None` usa el default de la config del proveedor;
    pasarlo explícito permite cambiar de modelo por llamada.
    """

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult: ...
