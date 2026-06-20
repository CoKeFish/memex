"""OpenAIClient — parsing, json mode, override de modelo, retries (respx). Espejo de DeepSeek:
mismo dialecto `/chat/completions`, mismos parsers (`_openai`), mismo retry."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.llm.client import ChatMessage, LLMClient, LLMError, LLMQuotaError
from memex.llm.config import LLMConfig
from memex.llm.openai import OpenAIClient, OpenAIError, openai_config

BASE_URL = "https://api.openai.com/v1"
CHAT = "/chat/completions"


def _client() -> OpenAIClient:
    cfg = LLMConfig(
        api_key=SecretStr("TKN"),
        base_url=BASE_URL,
        default_model="gpt-4o",
        backoff_base=0.001,
        max_retries=3,
    )
    return OpenAIClient(cfg)


def _ok(
    content: str = "hola",
    *,
    model: str = "gpt-4o",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "usage": usage
        or {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }


@pytest.mark.asyncio
async def test_complete_parses_content_and_usage() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok())
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "di hola")])
        assert route.called
        assert r.content == "hola"
        assert r.model == "gpt-4o"
        assert r.finish_reason == "stop"
        assert r.usage.prompt_tokens == 100
        assert r.usage.completion_tokens == 20
        assert r.cost_usd >= Decimal("0")
        assert r.latency_ms >= 0


@pytest.mark.asyncio
async def test_json_object_sets_response_format() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok(content='{"k":1}'))
        async with _client() as c:
            await c.complete(
                [ChatMessage("user", "responde en json")], response_format="json_object"
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_text_mode_omits_response_format() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok())
        async with _client() as c:
            await c.complete([ChatMessage("user", "x")])
        sent = json.loads(route.calls[0].request.content)
        assert "response_format" not in sent


@pytest.mark.asyncio
async def test_model_and_params_override_per_call() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok(model="gpt-4o-mini"))
        async with _client() as c:
            r = await c.complete(
                [ChatMessage("system", "s"), ChatMessage("user", "x")],
                model="gpt-4o-mini",
                temperature=0.2,
                max_tokens=50,
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == "gpt-4o-mini"
        assert sent["temperature"] == 0.2
        assert sent["max_tokens"] == 50
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]
        assert r.model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_reasoning_model_uses_max_completion_tokens_and_omits_temperature() -> None:
    # gpt-5.x (razonamiento) rechaza max_tokens y temperature≠1 → el cliente traduce el dialecto.
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok(model="gpt-5.5"))
        async with _client() as c:
            await c.complete(
                [ChatMessage("user", "x")],
                model="gpt-5.5",
                temperature=0.0,
                max_tokens=1024,
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["max_completion_tokens"] >= 16000  # headroom para tokens de pensamiento
        assert "max_tokens" not in sent
        assert "temperature" not in sent


@pytest.mark.asyncio
async def test_chat_variant_keeps_classic_params() -> None:
    # las variantes '-chat' se comportan como gpt-4o: max_tokens + temperature.
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok(model="gpt-5.5-chat-latest"))
        async with _client() as c:
            await c.complete(
                [ChatMessage("user", "x")],
                model="gpt-5.5-chat-latest",
                temperature=0.2,
                max_tokens=1024,
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["max_tokens"] == 1024
        assert sent["temperature"] == 0.2
        assert "max_completion_tokens" not in sent


@pytest.mark.asyncio
async def test_bearer_token_in_header_not_url() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(json=_ok())
        async with _client() as c:
            await c.complete([ChatMessage("user", "x")])
        req = router.calls[0].request
        assert req.headers["authorization"] == "Bearer TKN"
        assert "TKN" not in str(req.url)


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(401, text="unauthorized")
        async with _client() as c:
            with pytest.raises(OpenAIError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert exc.value.status_code == 401
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=_ok(content="ok")),
            ]
        )
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == "ok"
        assert router.calls.call_count == 3


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
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == "ok"
        assert router.calls.call_count == 2  # 429 retryable (rate-limit), no 4xx inmediato


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(OpenAIError):
                await c.complete([ChatMessage("user", "x")])
        assert route.call_count == 4  # max_retries=3 → 4 intentos


@pytest.mark.asyncio
async def test_402_raises_quota_error_no_retry() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(402, text="insufficient balance")
        async with _client() as c:
            with pytest.raises(LLMQuotaError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert exc.value.status_code == 402
        assert router.calls.call_count == 1


def test_openai_config_uses_canonical_key_and_base() -> None:
    cfg = openai_config({"OPENAI_API_KEY": "K"}, default_model="gpt-4o-mini")
    assert cfg.api_key.get_secret_value() == "K"
    assert cfg.base_url == BASE_URL
    assert cfg.default_model == "gpt-4o-mini"


def test_openai_client_satisfies_llmclient_protocol() -> None:
    assert issubclass(OpenAIClient, LLMClient)


def test_openai_error_subclasses_llm_error() -> None:
    assert issubclass(OpenAIError, LLMError)
