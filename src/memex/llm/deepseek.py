"""DeepSeekClient — el ÚNICO lugar que habla HTTP con DeepSeek.

Aísla al vendor detrás del Protocol `LLMClient`: los callers consumen `LLMResult`,
nunca URLs ni shapes de DeepSeek. Cambiar de proveedor = otra clase que implementa
`LLMClient`; este módulo no se entera.

Usa httpx **asíncrono** (`AsyncClient`) — NO el SDK `openai`/`deepseek`. El
patrón de retry/backoff está espejado de `ApifyClient._request` pero con
`asyncio.sleep`: reintenta 429 (rate-limit) + 5xx + errores de red con backoff
exponencial; 402 (saldo agotado) levanta `LLMQuotaError` para ABORTAR la corrida;
otro 4xx levanta `DeepSeekError` inmediato.

A diferencia del run-start pago no-idempotente de Apify, el POST a
`/chat/completions` **sí** se reintenta en 5xx/red: es la práctica estándar para
LLMs (un 5xx normalmente no produjo —ni facturó— una completion).

ADR-001-style: solo importa httpx, pydantic-resueltos y `memex.logging`; nunca
internals de memex (db/api/inbox).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from memex.llm import breaker
from memex.llm._openai import parse_choice, parse_usage
from memex.llm._openai_stream import stream_openai_tools
from memex.llm.client import (
    ChatMessage,
    LLMDeadlineError,
    LLMError,
    LLMQuotaError,
    LLMResult,
    ResponseFormat,
)
from memex.llm.config import LLMConfig
from memex.llm.pricing import ModelPricing, compute_cost, load_pricing
from memex.llm.tools import AgentMessage, StreamEvent, ToolSpec
from memex.logging import get_logger

_CHAT_PATH = "/chat/completions"
_BODY_PREVIEW_MAX = 500


class DeepSeekError(LLMError):
    """Raised cuando DeepSeek devuelve un error (4xx, o 5xx/red tras agotar retries)."""


class DeepSeekClient:
    """Cliente HTTP async mínimo para la API de DeepSeek (compatible OpenAI).

    Implementa el Protocol `LLMClient`. El token va en `Authorization: Bearer`
    (nunca en la URL). Construir con `client` inyectado para tests (respx), o dejar
    que cree el suyo.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: httpx.AsyncClient | None = None,
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self._config = config
        self._log = get_logger("memex.llm.deepseek")
        # Resolver la tabla de pricing UNA vez (defaults + overrides de MEMEX_LLM_PRICING).
        # Inyectable para tests; por default lee el entorno.
        self._pricing = pricing if pricing is not None else load_pricing()

        headers = {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
        )
        self._owns_client = client is None
        self._breaker_key = f"deepseek:{config.base_url}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DeepSeekClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        model_name = model or self._config.default_model
        body: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        # ¿El proveedor está cortado por venir fallando? Ni salimos a la red.
        corte = breaker.seconds_left(self._breaker_key)
        if corte:
            raise LLMDeadlineError(
                0,
                f"deepseek cortado por fallas seguidas ({corte:.0f} s mas)",
            )
        started = time.monotonic()
        try:
            # Tope de reloj de pared: un proveedor que acepta el request y no lo
            # termina nunca no puede clavar la corrida (ver `deadline_s`).
            async with asyncio.timeout(self._config.deadline_s):
                resp = await self._request("POST", _CHAT_PATH, json=body)
        except TimeoutError as e:
            waited = time.monotonic() - started
            self._log.warning(
                "llm.deepseek.request.deadline",
                model=model_name,
                deadline_s=self._config.deadline_s,
                waited_s=round(waited, 1),
            )
            breaker.record_failure(self._breaker_key)
            raise LLMDeadlineError(
                0,
                f"deepseek no respondio en {self._config.deadline_s:.0f} s",
            ) from e
        breaker.record_success(self._breaker_key)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = resp.json()
        content, finish_reason = parse_choice(data)
        usage = parse_usage(data.get("usage") if isinstance(data, dict) else None)
        # `at` = momento de la completion (UTC) para evaluar la ventana off-peak.
        at = datetime.now(UTC)
        cost = compute_cost(model_name, usage, pricing=self._pricing, at=at)

        self._log.info(
            "llm.deepseek.complete",
            model=model_name,
            response_format=response_format,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=str(cost),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
        return LLMResult(
            content=content,
            model=model_name,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )

    def stream_tools(
        self,
        messages: Sequence[AgentMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Chat con herramientas en STREAMING (Protocol `ToolStreamClient`, ver `memex.llm.tools`).
        Mismo endpoint y retry que `complete`; los eventos salen a medida que llegan los chunks."""
        model_name = model or self._config.default_model
        body: dict[str, Any] = {
            "model": model_name,
            "messages": [m.as_openai() for m in messages],
        }
        if tools:
            body["tools"] = [t.as_openai() for t in tools]
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return stream_openai_tools(
            self._client,
            path=_CHAT_PATH,
            body=body,
            model=model_name,
            pricing=self._pricing,
            error_cls=DeepSeekError,
            max_retries=self._config.max_retries,
            backoff_base=self._config.backoff_base,
            log=self._log,
            log_event="llm.deepseek.stream",
        )

    async def _request(self, method: str, path: str, *, json: Any) -> httpx.Response:
        """HTTP con retry de 429/5xx/red; 402 → LLMQuotaError (aborta); otro 4xx inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "llm.deepseek.request.network_error",
                    path=path,
                    # `str(ReadTimeout())` es vacío: sin la clase, el aviso no dice nada.
                    exc_type=type(e).__name__,
                    exc=str(e),
                    attempt=attempt,
                )
            else:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = DeepSeekError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "llm.deepseek.request.retryable", status=resp.status_code, attempt=attempt
                    )
                elif resp.status_code == 402:
                    # Saldo agotado: no tiene sentido reintentar ni seguir la corrida sin saldo.
                    raise LLMQuotaError(
                        402, "insufficient balance", body=resp.text[:_BODY_PREVIEW_MAX] or None
                    )
                elif 400 <= resp.status_code < 500:
                    raise DeepSeekError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, DeepSeekError):
            raise last_exc
        raise DeepSeekError(0, f"network error on {method} {path}") from last_exc
