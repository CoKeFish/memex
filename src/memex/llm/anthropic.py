"""AnthropicClient — el ÚNICO lugar que habla HTTP con la Messages API de Anthropic.

Segundo proveedor detrás del Protocol `LLMClient` (el primero es `DeepSeekClient`): los
callers consumen `LLMResult`, nunca URLs ni shapes de Anthropic. Mismo patrón: httpx
**asíncrono** sin SDK, retry/backoff espejado de `DeepSeekClient._request`.

Diferencias de vendor encapsuladas acá (los callers no se enteran):

- Endpoint `POST /v1/messages`; auth por header `x-api-key` + `anthropic-version` (no Bearer).
- `max_tokens` es OBLIGATORIO en el body → si el caller no lo pasa se usa un default.
- Los turnos `system` no van en `messages`: se concatenan al campo top-level `system`.
- En Opus 4.8 los sampling params (`temperature`/`top_p`/`top_k`) y `thinking` configurado
  devuelven 400 → NUNCA se envían. `temperature` se acepta y se IGNORA. El JSON se pide por
  prompt (sin modo nativo): `response_format="json_object"` no viaja en el body pero activa
  el saneo de la salida (`normalize_json_output`: extrae el JSON de fences/prosa solo si
  parsea; si no, pasa crudo y el parser del caller degrada seguro).
- `stop_reason` se normaliza a la convención del repo (`end_turn`→"stop", `max_tokens`→
  "length") para que el `_OK_FINISH = {"stop"}` de los workers funcione sin cambios;
  `refusal` (clasificadores de seguridad) pasa crudo → los workers lo tratan como no-ok.
- Usage: `cache_read_input_tokens` son los hits; `input_tokens` (no cacheados) +
  `cache_creation_input_tokens` (escritura de cache) se cuentan como miss. El premium 1.25x
  de la escritura de cache NO se modela (subestima ~25% SOLO esos tokens; el gate no usa
  cache_control, así que en la práctica son 0).
- Saldo agotado: Anthropic responde **400** con "credit balance is too low" (no 402 como
  DeepSeek) → se mapea a `LLMQuotaError` para ABORTAR la corrida igual que con DeepSeek.
- 529 (overloaded) es retryable además de 429/5xx.

ADR-001-style: solo importa httpx y `memex.logging`; nunca internals de memex (db/api/inbox).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from memex.llm._json import normalize_json_output
from memex.llm.client import (
    ChatMessage,
    LLMError,
    LLMQuotaError,
    LLMResult,
    LLMUsage,
    ResponseFormat,
)
from memex.llm.config import LLMConfig
from memex.llm.pricing import ModelPricing, compute_cost, load_pricing
from memex.logging import get_logger

_MESSAGES_PATH = "/v1/messages"
_BODY_PREVIEW_MAX = 500

#: Nombre canónico de la env var con la API key de Anthropic (Doppler, config shared).
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
#: Modelo default del gate de relevancia (decisión del dueño: modelo superior, no flash).
_DEFAULT_MODEL = "claude-opus-4-8"
_ANTHROPIC_VERSION = "2023-06-01"
#: La Messages API EXIGE max_tokens; default conservador si el caller no lo pasa.
_DEFAULT_MAX_TOKENS = 1024
#: Opus es más lento que flash: read timeout más generoso que el default de LLMConfig.
_DEFAULT_TIMEOUT_S = 120.0

#: Normalización de stop_reason a la convención del repo (la de OpenAI/DeepSeek, que es la
#: que los workers chequean). Valores no mapeados pasan crudos (p. ej. "refusal").
_STOP_REASON_MAP = {"end_turn": "stop", "max_tokens": "length"}

#: Marcador del 400 de saldo agotado de Anthropic (case-insensitive).
_QUOTA_MARKER = "credit balance"


class AnthropicError(LLMError):
    """Raised cuando Anthropic devuelve un error (4xx, o 5xx/red tras agotar retries)."""


def anthropic_config(env: Mapping[str, str] | None = None) -> LLMConfig:
    """`LLMConfig` para Anthropic: ANTHROPIC_API_KEY + base/model/timeout propios."""
    return LLMConfig.from_env(
        env,
        api_key_env=_API_KEY_ENV,
        base_url=_DEFAULT_BASE_URL,
        default_model=_DEFAULT_MODEL,
    ).model_copy(update={"timeout_s": _DEFAULT_TIMEOUT_S})


class AnthropicClient:
    """Cliente HTTP async mínimo para la Messages API de Anthropic.

    Implementa el Protocol `LLMClient`. La key va en el header `x-api-key` (nunca en la
    URL). Construir con `client` inyectado para tests (respx), o dejar que cree el suyo.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        client: httpx.AsyncClient | None = None,
        pricing: Mapping[str, ModelPricing] | None = None,
    ) -> None:
        self._config = config
        self._log = get_logger("memex.llm.anthropic")
        self._pricing = pricing if pricing is not None else load_pricing()

        headers = {
            "x-api-key": config.api_key.get_secret_value(),
            "anthropic-version": _ANTHROPIC_VERSION,
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

    async def __aenter__(self) -> AnthropicClient:
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

        # Los system van al campo top-level; user/assistant al array. `temperature` y
        # `response_format` se ignoran a propósito (ver docstring del módulo).
        system_parts = [m.content for m in messages if m.role == "system"]
        chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        body: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "messages": chat,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)

        started = time.monotonic()
        resp = await self._request("POST", _MESSAGES_PATH, json=body)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = resp.json()
        content, finish_reason = _parse_message(data)
        usage = _parse_usage(data.get("usage") if isinstance(data, dict) else None)
        cost = compute_cost(model_name, usage, pricing=self._pricing, at=datetime.now(UTC))

        # JSON por prompt (sin modo nativo): si el caller pidió JSON, se extrae de eventuales
        # fences/prosa — solo si el candidato parsea; si no, pasa crudo (el parser del caller
        # degrada seguro). Mismo saneo que CodexClient; DeepSeek no lo necesita (modo nativo).
        if response_format == "json_object":
            normalized = normalize_json_output(content)
            if normalized != content:
                self._log.info(
                    "llm.anthropic.json_normalized",
                    raw_chars=len(content),
                    normalized_chars=len(normalized),
                )
                content = normalized

        self._log.info(
            "llm.anthropic.complete",
            model=model_name,
            response_format=response_format,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
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
        """HTTP con retry de 429/529/5xx/red; 400 de saldo → LLMQuotaError; otro 4xx inmediato."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                self._log.warning(
                    "llm.anthropic.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                retryable = resp.status_code in (429, 529) or 500 <= resp.status_code < 600
                if retryable:
                    last_exc = AnthropicError(
                        resp.status_code,
                        f"server/rate error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "llm.anthropic.request.retryable", status=resp.status_code, attempt=attempt
                    )
                elif 400 <= resp.status_code < 500:
                    body_preview = resp.text[:_BODY_PREVIEW_MAX] or None
                    if resp.status_code == 400 and _QUOTA_MARKER in resp.text.lower():
                        # Saldo agotado (Anthropic lo reporta como 400, no 402): abortar corrida.
                        raise LLMQuotaError(400, "insufficient credit balance", body=body_preview)
                    raise AnthropicError(
                        resp.status_code, f"client error {resp.status_code}", body=body_preview
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, AnthropicError):
            raise last_exc
        raise AnthropicError(0, f"network error on {method} {path}") from last_exc


def _parse_message(data: Any) -> tuple[str, str | None]:
    """Extrae texto + stop_reason normalizado de una respuesta de la Messages API.

    El `content` es una lista de bloques tipados; se concatenan SOLO los `type=="text"`
    (un `refusal` pre-output llega con content vacío → content ""). Shape inesperado →
    `AnthropicError` (no se adivina).
    """
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise AnthropicError(0, "unexpected response shape: missing content list")
    parts = [
        str(block.get("text", ""))
        for block in data["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    raw_stop = data.get("stop_reason")
    finish_reason = _STOP_REASON_MAP.get(raw_stop, raw_stop) if raw_stop is not None else None
    return "".join(parts), finish_reason


def _parse_usage(raw: Any) -> LLMUsage:
    """Mapea el usage de Anthropic a `LLMUsage` (shape distinto al de OpenAI/DeepSeek).

    `input_tokens` son SOLO los tokens no cacheados; el prompt total es
    input + cache_creation + cache_read. Hits = cache_read; el resto cuenta como miss.
    """
    if not isinstance(raw, dict):
        return LLMUsage(0, 0, 0)
    uncached = int(raw.get("input_tokens", 0) or 0)
    cache_creation = int(raw.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(raw.get("cache_read_input_tokens", 0) or 0)
    output = int(raw.get("output_tokens", 0) or 0)
    prompt = uncached + cache_creation + cache_read
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=output,
        total_tokens=prompt + output,
        cache_hit_tokens=cache_read,
        cache_miss_tokens=uncached + cache_creation,
    )
