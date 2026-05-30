"""OpenAIVisionClient — el ÚNICO lugar que habla HTTP con el proveedor de visión.

Aísla al vendor detrás del Protocol `OCRClient`: los callers consumen `OcrResult`, nunca URLs
ni shapes del proveedor. Cambiar de proveedor de OCR = otra clase que implementa `OCRClient`.

Usa httpx **asíncrono** (`AsyncClient`) contra un endpoint OpenAI-compatible
(`POST {base_url}/chat/completions`) mandando la imagen como bloque `image_url` con un data-URI
base64. El patrón de retry/backoff es el mismo de `DeepSeekClient` (5xx/red reintenta, 4xx
inmediato). Parsea con los helpers compartidos `memex.llm._openai` (mismo dialecto).

ADR-001-style: solo importa httpx, pydantic-resueltos, `memex.llm` (primitivos) y
`memex.logging`; nunca internals de memex (db/api/inbox).
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx

from memex.llm._openai import parse_choice, parse_usage
from memex.logging import get_logger
from memex.ocr.client import OcrError, OcrResult
from memex.ocr.config import OcrConfig
from memex.ocr.pricing import compute_ocr_cost
from memex.ocr.prompt import OCR_SYSTEM_PROMPT, OCR_USER_INSTRUCTION

_CHAT_PATH = "/chat/completions"
_BODY_PREVIEW_MAX = 500
#: Tope de tokens de salida — una transcripción puede ser larga (recibos densos, tablas). Si se
#: agota, el proveedor devuelve finish_reason='length' y el worker marca el asset como truncado.
_MAX_TOKENS = 8192


class OpenAIVisionClient:
    """Cliente HTTP async mínimo para un proveedor de OCR por visión (OpenAI-compatible).

    Implementa el Protocol `OCRClient`. El token va en `Authorization: Bearer` (nunca en la URL).
    Construir con `client` inyectado para tests (respx), o dejar que cree el suyo.
    """

    def __init__(self, config: OcrConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._log = get_logger("memex.ocr.openai_vision")

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

    async def __aenter__(self) -> OpenAIVisionClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def ocr_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        model: str | None = None,
    ) -> OcrResult:
        model_name = model or self._config.default_model
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{content_type};base64,{b64}"
        body: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": OCR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OCR_USER_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
            "temperature": 0.0,
            "max_tokens": _MAX_TOKENS,
        }

        started = time.monotonic()
        resp = await self._request("POST", _CHAT_PATH, json=body)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = resp.json()
        content, finish_reason = parse_choice(data)
        usage = parse_usage(data.get("usage") if isinstance(data, dict) else None)
        cost = compute_ocr_cost(model_name, usage)

        self._log.info(
            "ocr.vision.complete",
            model=model_name,
            content_type=content_type,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=str(cost),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            chars=len(content),
        )
        return OcrResult(
            text=content,
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
                    "ocr.vision.request.network_error", path=path, exc=str(e), attempt=attempt
                )
            else:
                if 500 <= resp.status_code < 600:
                    last_exc = OcrError(
                        resp.status_code,
                        f"server error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                    self._log.warning(
                        "ocr.vision.request.5xx", status=resp.status_code, attempt=attempt
                    )
                elif 400 <= resp.status_code < 500:
                    raise OcrError(
                        resp.status_code,
                        f"client error {resp.status_code}",
                        body=resp.text[:_BODY_PREVIEW_MAX] or None,
                    )
                else:
                    return resp

            if attempt < self._config.max_retries:
                await asyncio.sleep(self._config.backoff_base * (2**attempt))

        if isinstance(last_exc, OcrError):
            raise last_exc
        raise OcrError(0, f"network error on {method} {path}") from last_exc
