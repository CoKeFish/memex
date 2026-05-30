"""DeepSeekClient — el ÚNICO lugar que habla HTTP con DeepSeek.

Aísla al vendor detrás del Protocol `LLMClient`: los callers consumen `LLMResult`,
nunca URLs ni shapes de DeepSeek. Cambiar de proveedor = otra clase que implementa
`LLMClient`; este módulo no se entera.

Usa httpx **asíncrono** (`AsyncClient`) — NO el SDK `openai`/`deepseek`. El
patrón de retry/backoff está espejado de `ApifyClient._request` pero con
`asyncio.sleep`: reintenta 5xx + errores de red con backoff exponencial; 4xx
levanta `DeepSeekError` inmediato.

A diferencia del run-start pago no-idempotente de Apify, el POST a
`/chat/completions` **sí** se reintenta en 5xx/red: es la práctica estándar para
LLMs (un 5xx normalmente no produjo —ni facturó— una completion).

ADR-001-style: solo importa httpx, pydantic-resueltos y `memex.logging`; nunca
internals de memex (db/api/inbox).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import httpx

from memex.llm.client import (
    ChatMessage,
    LLMError,
    LLMResult,
    LLMUsage,
    ResponseFormat,
)
from memex.llm.config import LLMConfig
from memex.llm.pricing import compute_cost
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

    def __init__(self, config: LLMConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._log = get_logger("memex.llm.deepseek")

        headers = {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s),
        )
        self._owns_client = client is None

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

        started = time.monotonic()
        resp = await self._request("POST", _CHAT_PATH, json=body)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = resp.json()
        content, finish_reason = _parse_choice(data)
        usage = _parse_usage(data.get("usage") if isinstance(data, dict) else None)
        cost = compute_cost(model_name, usage)

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

    async def _request(self, method: str, path: str, *, json: Any) -> httpx.Response:
        """HTTP con retry de 5xx/red (backoff exponencial); 4xx → error inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "llm.deepseek.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if 500 <= resp.status_code < 600:
                    last_exc = DeepSeekError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "llm.deepseek.request.5xx", status=resp.status_code, attempt=attempt
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


def _parse_choice(data: Any) -> tuple[str, str | None]:
    """Extrae (content, finish_reason) de `choices[0]` defensivamente."""
    if not isinstance(data, dict):
        return "", None
    choices = data.get("choices")
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
        return "", None
    first: dict[str, Any] = choices[0]
    message = first.get("message")
    content = str(message.get("content") or "") if isinstance(message, dict) else ""
    raw_reason = first.get("finish_reason")
    finish_reason = raw_reason if isinstance(raw_reason, str) else None
    return content, finish_reason


def _parse_usage(raw: Any) -> LLMUsage:
    """Mapea el objeto `usage` de DeepSeek a `LLMUsage` (defensivo ante faltantes)."""
    u: dict[str, Any] = raw if isinstance(raw, dict) else {}
    prompt = _as_int(u.get("prompt_tokens"))
    completion = _as_int(u.get("completion_tokens"))
    total = _as_int(u.get("total_tokens")) or (prompt + completion)
    hit = _as_int(u.get("prompt_cache_hit_tokens"))
    miss_raw = u.get("prompt_cache_miss_tokens")
    miss = _as_int(miss_raw) if miss_raw is not None else max(prompt - hit, 0)
    details = u.get("completion_tokens_details")
    reasoning = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        reasoning_tokens=reasoning,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
