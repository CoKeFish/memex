"""OpenAIVisionClient con respx (sin red). Espeja tests/llm/test_deepseek.py.

Cubre: forma del request (imagen como bloque image_url data-URI), parseo de content/usage,
bearer fuera de la URL, y la lógica de retry (4xx inmediato / 5xx retry / red retry).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.ocr.client import OCRClient, OcrError, OcrResult
from memex.ocr.config import OcrConfig
from memex.ocr.openai_vision import OpenAIVisionClient

BASE_URL = "https://vision.example.com/v1"
CHAT = "/chat/completions"


def _client() -> OpenAIVisionClient:
    cfg = OcrConfig(
        api_key=SecretStr("TKN"),
        base_url=BASE_URL,
        default_model="vision-mini",
        backoff_base=0.001,
        max_retries=3,
    )
    return OpenAIVisionClient(cfg)


def _ok(content: str = "TEXTO OCR", *, model: str = "vision-mini") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 800, "completion_tokens": 40, "total_tokens": 840},
    }


def test_satisfies_protocol() -> None:
    assert issubclass(OpenAIVisionClient, OCRClient)


@pytest.mark.asyncio
async def test_ocr_image_builds_vision_request_and_parses() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok())
        async with _client() as c:
            r: OcrResult = await c.ocr_image(image_bytes=b"\x89PNG", content_type="image/png")

        assert route.called
        assert r.text == "TEXTO OCR"
        assert r.model == "vision-mini"
        assert r.usage.prompt_tokens == 800
        assert r.finish_reason == "stop"

        sent = json.loads(route.calls[0].request.content)
        # El segundo turno (user) lleva content como LISTA de bloques: texto + image_url.
        user_content = sent["messages"][1]["content"]
        assert isinstance(user_content, list)
        kinds = {blk["type"] for blk in user_content}
        assert kinds == {"text", "image_url"}
        img = next(b for b in user_content if b["type"] == "image_url")
        assert img["image_url"]["url"].startswith("data:image/png;base64,")
        # Bearer en header, nunca en la URL.
        assert route.calls[0].request.headers["Authorization"] == "Bearer TKN"


@pytest.mark.asyncio
async def test_model_override_per_call() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok(model="big-vision"))
        async with _client() as c:
            await c.ocr_image(image_bytes=b"x", content_type="image/jpeg", model="big-vision")
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == "big-vision"


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(401, text="unauthorized")
        async with _client() as c:
            with pytest.raises(OcrError) as exc:
                await c.ocr_image(image_bytes=b"x", content_type="image/png")
        assert exc.value.status_code == 401
        assert router.calls.call_count == 1  # sin retries


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=_ok(content="ok")),
            ]
        )
        async with _client() as c:
            r = await c.ocr_image(image_bytes=b"x", content_type="image/png")
        assert r.text == "ok"
        assert router.calls.call_count == 2


@pytest.mark.asyncio
async def test_429_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json=_ok(content="ok")),
            ]
        )
        async with _client() as c:
            r = await c.ocr_image(image_bytes=b"x", content_type="image/png")
        assert r.text == "ok"
        assert router.calls.call_count == 2  # 429 es retryable (rate-limit), no 4xx inmediato


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(OcrError):
                await c.ocr_image(image_bytes=b"x", content_type="image/png")
        assert route.call_count == 4  # max_retries=3 → 4 intentos
