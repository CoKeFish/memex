"""OpenAIClient — el ÚNICO lugar que habla HTTP con la API de OpenAI (`/chat/completions`).

Espejo de `DeepSeekClient`: la API de OpenAI y la de DeepSeek hablan el MISMO dialecto
(`/chat/completions`, `choices[0].message.content` + `usage`), así que comparten los parsers
(`memex.llm._openai`) y el mismo retry. A diferencia de `codex` (CLI agéntico, host-side, ~25s por
llamada), esto es la API DIRECTA: rápida y con métricas de tokens. Aísla a OpenAI detrás del
Protocol `LLMClient`. Key vía `OPENAI_API_KEY` (Doppler), sin prefijo `MEMEX_` (nombre canónico de
OpenAI). ADR-001-style: solo httpx + `memex.logging`; nunca internals de memex.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from memex.llm._openai import parse_choice, parse_usage
from memex.llm.client import (
    ChatMessage,
    LLMError,
    LLMQuotaError,
    LLMResult,
    ResponseFormat,
)
from memex.llm.config import LLMConfig
from memex.llm.pricing import ModelPricing, compute_cost, load_pricing
from memex.logging import get_logger

_CHAT_PATH = "/chat/completions"
_BODY_PREVIEW_MAX = 500
_API_KEY_ENV = "OPENAI_API_KEY"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
#: Default override-able por llamada (`complete(..., model=)`) o por consumer (`settings.model`).
_DEFAULT_MODEL = "gpt-4o"

#: Modelos de razonamiento (GPT-5 / o-series, salvo las variantes '-chat'): NO aceptan `temperature`
#: distinta del default (1) y usan `max_completion_tokens` en vez de `max_tokens`. Además los tokens
#: de pensamiento se cobran DENTRO de ese budget → un cap chico los trunca antes de emitir el JSON
#: (finish_reason='length', content vacío); por eso se les da headroom.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_REASONING_MIN_COMPLETION = 16000


def _is_reasoning_model(model: str) -> bool:
    """`True` si el modelo usa el dialecto de razonamiento (GPT-5 / o-series, salvo '-chat')."""
    m = model.lower()
    return m.startswith(_REASONING_PREFIXES) and "chat" not in m


def openai_config(
    env: Mapping[str, str] | None = None, *, default_model: str | None = None
) -> LLMConfig:
    """`LLMConfig` para OpenAI: `OPENAI_API_KEY` + base/model propios."""
    return LLMConfig.from_env(
        env,
        api_key_env=_API_KEY_ENV,
        base_url=_DEFAULT_BASE_URL,
        default_model=default_model or _DEFAULT_MODEL,
    )


class OpenAIError(LLMError):
    """Raised cuando OpenAI devuelve un error (4xx, o 5xx/red tras agotar retries)."""


class OpenAIClient:
    """Cliente HTTP async mínimo para la API de OpenAI. Implementa el Protocol `LLMClient`. El token
    va en `Authorization: Bearer` (nunca en la URL). `client` inyectable para tests (respx)."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: httpx.AsyncClient | None = None,
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self._config = config
        self._log = get_logger("memex.llm.openai")
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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAIClient:
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
        if _is_reasoning_model(model_name):
            # razonamiento: `temperature` fija (1) → se omite; budget con headroom de pensamiento.
            body["max_completion_tokens"] = max(max_tokens or 0, _REASONING_MIN_COMPLETION)
        else:
            if temperature is not None:
                body["temperature"] = temperature
            if max_tokens is not None:
                body["max_tokens"] = max_tokens

        started = time.monotonic()
        resp = await self._request("POST", _CHAT_PATH, json=body)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = resp.json()
        content, finish_reason = parse_choice(data)
        usage = parse_usage(data.get("usage") if isinstance(data, dict) else None)
        at = datetime.now(UTC)
        cost = compute_cost(model_name, usage, pricing=self._pricing, at=at)

        self._log.info(
            "llm.openai.complete",
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

    async def _request(self, method: str, path: str, *, json: Any) -> httpx.Response:
        """HTTP con retry de 429/5xx/red; 401/otro 4xx inmediato. (OpenAI manda 429 también para
        cuota agotada → se reintenta y luego falla como `OpenAIError`, no aborta la corrida)."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "llm.openai.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_exc = OpenAIError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "llm.openai.request.retryable", status=resp.status_code, attempt=attempt
                    )
                elif resp.status_code == 402:
                    raise LLMQuotaError(
                        402, "insufficient balance", body=resp.text[:_BODY_PREVIEW_MAX] or None
                    )
                elif 400 <= resp.status_code < 500:
                    raise OpenAIError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, OpenAIError):
            raise last_exc
        raise OpenAIError(0, f"network error on {method} {path}") from last_exc
