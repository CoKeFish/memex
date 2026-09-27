"""DeepSeekClient — parsing, costo, json mode, override de modelo, retries (respx)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.llm.client import (
    ChatMessage,
    LLMClient,
    LLMDeadlineError,
    LLMError,
    LLMQuotaError,
)
from memex.llm.config import LLMConfig
from memex.llm.deepseek import DeepSeekClient, DeepSeekError

BASE_URL = "https://api.deepseek.com"
CHAT = "/chat/completions"


def _client() -> DeepSeekClient:
    cfg = LLMConfig(
        api_key=SecretStr("TKN"),
        base_url=BASE_URL,
        default_model="deepseek-chat",
        backoff_base=0.001,
        max_retries=3,
    )
    return DeepSeekClient(cfg)


def _ok(
    content: str = "hola",
    *,
    model: str = "deepseek-chat",
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
            "prompt_cache_hit_tokens": 60,
            "prompt_cache_miss_tokens": 40,
        },
    }


@pytest.mark.asyncio
async def test_complete_parses_content_usage_and_cost() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).respond(json=_ok())
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "di hola")])
        assert route.called
        assert r.content == "hola"
        assert r.model == "deepseek-chat"
        assert r.finish_reason == "stop"
        assert r.usage.prompt_tokens == 100
        assert r.usage.completion_tokens == 20
        assert r.usage.cache_hit_tokens == 60
        assert r.usage.cache_miss_tokens == 40
        # deepseek-chat = v4-flash: (0.0028*60 + 0.14*40 + 0.28*20)/1e6 = 11.368e-6 → 0.000011
        # 60 hit + 40 miss + 20 out: 0.000036 a tarifa pico; la mitad si el test corre dentro de la
        # ventana off-peak de DeepSeek (16:30-00:30 UTC), porque `complete` tasa con la hora real.
        assert r.cost_usd in (Decimal("0.000036"), Decimal("0.000018"))
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
        route = router.post(CHAT).respond(json=_ok(model="deepseek-v4-pro"))
        async with _client() as c:
            r = await c.complete(
                [ChatMessage("system", "s"), ChatMessage("user", "x")],
                model="deepseek-v4-pro",
                temperature=0.2,
                max_tokens=50,
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == "deepseek-v4-pro"
        assert sent["temperature"] == 0.2
        assert sent["max_tokens"] == 50
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]
        assert r.model == "deepseek-v4-pro"


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
async def test_usage_without_cache_split_defaults_to_miss() -> None:
    usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(json=_ok(usage=usage))
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.usage.cache_hit_tokens == 0
        assert r.usage.cache_miss_tokens == 50


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(401, text="unauthorized")
        async with _client() as c:
            with pytest.raises(DeepSeekError) as exc:
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
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json=_ok(content="ok")),
            ]
        )
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == "ok"
        assert router.calls.call_count == 3  # 429 es retryable (rate-limit), no 4xx inmediato


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(DeepSeekError):
                await c.complete([ChatMessage("user", "x")])
        # max_retries=3 → 4 intentos antes de rendirse
        assert route.call_count == 4


@pytest.mark.asyncio
async def test_402_raises_quota_error_no_retry() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(402, text="Insufficient Balance")
        async with _client() as c:
            with pytest.raises(LLMQuotaError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert exc.value.status_code == 402
        assert router.calls.call_count == 1  # 402 = saldo agotado, no se reintenta


@pytest.mark.asyncio
async def test_timeout_separates_connect_from_read() -> None:
    async with _client() as c:
        assert c._client.timeout.connect == 10.0  # connect corto, falla rápido
        assert c._client.timeout.read == 180.0  # read = budget de la completion (ventanas grandes)
        assert c._config.deadline_s == 180.0  # y el tope de reloj de pared de la llamada


def test_quota_error_subclasses_llm_error() -> None:
    assert issubclass(LLMQuotaError, LLMError)


def test_deepseek_client_satisfies_llmclient_protocol() -> None:
    assert issubclass(DeepSeekClient, LLMClient)


def test_deepseek_error_subclasses_llm_error() -> None:
    assert issubclass(DeepSeekError, LLMError)


@pytest.mark.asyncio
async def test_deadline_cuts_a_provider_that_never_answers() -> None:
    """El caso real: 200 en las cabeceras y el cuerpo no llega nunca.

    El timeout de lectura no sirve de red de seguridad —mide el hueco entre bytes y el keep-alive
    lo reinicia—, así que el tope es de reloj de pared. Y no se reintenta: el proveedor no está
    con un hipo de red, está sin responder.
    """
    import asyncio

    intentos = 0

    async def nunca(_request: httpx.Request) -> httpx.Response:
        nonlocal intentos
        intentos += 1
        await asyncio.sleep(3600)
        raise AssertionError("inalcanzable")

    cfg = LLMConfig(
        api_key=SecretStr("TKN"), base_url=BASE_URL, default_model="deepseek-chat", deadline_s=0.05
    )
    # `assert_all_called=False`: la ruta nunca llega a RESPONDER (ese es el punto del test).
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.post(CHAT).mock(side_effect=nunca)
        async with DeepSeekClient(cfg) as c:
            with pytest.raises(LLMDeadlineError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert intentos == 1  # sin reintentos: la cadena de fallback sigue sola
    assert exc.value.status_code == 0  # status 0 → `_should_fallback` lo deja pasar al siguiente


@pytest.mark.asyncio
async def test_deadline_covers_the_whole_call_not_each_attempt() -> None:
    """Reintentos lentos tampoco pasan del tope: el reloj corre sobre la llamada entera."""
    import asyncio

    async def lento(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        raise httpx.ConnectError("boom")

    cfg = LLMConfig(
        api_key=SecretStr("TKN"),
        base_url=BASE_URL,
        default_model="deepseek-chat",
        backoff_base=0.001,
        max_retries=100,
        deadline_s=0.2,
    )
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CHAT).mock(side_effect=lento)
        async with DeepSeekClient(cfg) as c:
            with pytest.raises(LLMDeadlineError):
                await c.complete([ChatMessage("user", "x")])
        assert 1 <= route.call_count < 100  # cortó a mitad de los reintentos


def test_deadline_error_subclasses_llm_error() -> None:
    assert issubclass(LLMDeadlineError, LLMError)
