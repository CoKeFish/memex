"""AnthropicClient — body sin sampling params, headers, usage/costo, stop_reason, retries."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.llm.anthropic import AnthropicClient, AnthropicError
from memex.llm.client import ChatMessage, LLMClient, LLMError, LLMQuotaError
from memex.llm.config import LLMConfig

BASE_URL = "https://api.anthropic.com"
MESSAGES = "/v1/messages"


def _client() -> AnthropicClient:
    cfg = LLMConfig(
        api_key=SecretStr("ANTKEY"),
        base_url=BASE_URL,
        default_model="claude-opus-4-8",
        backoff_base=0.001,
        max_retries=3,
    )
    return AnthropicClient(cfg)


def _ok(
    content: str = "hola",
    *,
    model: str = "claude-opus-4-8",
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg-1",
        "type": "message",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": stop_reason,
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.mark.asyncio
async def test_body_has_only_safe_fields_and_required_max_tokens() -> None:
    """En Opus 4.8 temperature/top_p/top_k/thinking devuelven 400 → NUNCA en el body.

    `temperature` y `response_format` del Protocol se aceptan y se ignoran; `max_tokens`
    es obligatorio en la Messages API → default si el caller no lo pasa.
    """
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MESSAGES).respond(json=_ok())
        async with _client() as c:
            await c.complete(
                [ChatMessage("user", "x")], response_format="json_object", temperature=0.7
            )
        sent = json.loads(route.calls[0].request.content)
        assert set(sent) == {"model", "max_tokens", "messages"}
        assert sent["max_tokens"] == 1024  # default: la API lo exige


@pytest.mark.asyncio
async def test_headers_use_x_api_key_and_version_not_bearer() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=_ok())
        async with _client() as c:
            await c.complete([ChatMessage("user", "x")])
        req = router.calls[0].request
        assert req.headers["x-api-key"] == "ANTKEY"
        assert req.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in req.headers
        assert "ANTKEY" not in str(req.url)


@pytest.mark.asyncio
async def test_system_messages_go_to_top_level_field() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MESSAGES).respond(json=_ok())
        async with _client() as c:
            await c.complete(
                [
                    ChatMessage("system", "sos un portero"),
                    ChatMessage("system", "responde JSON"),
                    ChatMessage("user", "x"),
                ]
            )
        sent = json.loads(route.calls[0].request.content)
        assert sent["system"] == "sos un portero\n\nresponde JSON"
        assert [m["role"] for m in sent["messages"]] == ["user"]


@pytest.mark.asyncio
async def test_usage_maps_cache_fields_and_cost() -> None:
    """prompt = input + cache_creation + cache_read; hit = cache_read; miss = resto."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 50,
    }
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=_ok(usage=usage))
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.usage.prompt_tokens == 180
        assert r.usage.cache_hit_tokens == 50
        assert r.usage.cache_miss_tokens == 130
        assert r.usage.completion_tokens == 20
        # opus-4-8: (0.50*50 + 5.00*130 + 25.00*20)/1e6 = 1175e-6 → 0.001175
        assert r.cost_usd == Decimal("0.001175")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("end_turn", "stop"), ("max_tokens", "length"), ("refusal", "refusal")],
)
async def test_stop_reason_is_normalized(raw: str, normalized: str) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=_ok(stop_reason=raw))
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.finish_reason == normalized


@pytest.mark.asyncio
async def test_multiple_text_blocks_are_concatenated() -> None:
    payload = _ok()
    payload["content"] = [
        {"type": "text", "text": "ho"},
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": "la"},
    ]
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=payload)
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == "hola"


@pytest.mark.asyncio
async def test_model_override_and_max_tokens_per_call() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MESSAGES).respond(json=_ok())
        async with _client() as c:
            await c.complete([ChatMessage("user", "x")], model="claude-opus-4-8", max_tokens=512)
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == "claude-opus-4-8"
        assert sent["max_tokens"] == 512


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(401, text="unauthorized")
        async with _client() as c:
            with pytest.raises(AnthropicError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert exc.value.status_code == 401
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_400_credit_balance_raises_quota_error_no_retry() -> None:
    """Anthropic reporta saldo agotado como 400 (no 402) → LLMQuotaError aborta la corrida."""
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API.",
        },
    }
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(400, json=body)
        async with _client() as c:
            with pytest.raises(LLMQuotaError):
                await c.complete([ChatMessage("user", "x")])
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_400_generic_raises_anthropic_error() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(400, text="bad request: temperature not supported")
        async with _client() as c:
            with pytest.raises(AnthropicError) as exc:
                await c.complete([ChatMessage("user", "x")])
        assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 529, 503])
async def test_retryable_statuses_then_succeed(status: int) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).mock(
            side_effect=[
                httpx.Response(status, text="busy"),
                httpx.Response(status, text="busy"),
                httpx.Response(200, json=_ok(content="ok")),
            ]
        )
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == "ok"
        assert router.calls.call_count == 3


@pytest.mark.asyncio
async def test_network_error_retries_then_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(MESSAGES).mock(side_effect=httpx.ConnectError("boom"))
        async with _client() as c:
            with pytest.raises(AnthropicError):
                await c.complete([ChatMessage("user", "x")])
        # max_retries=3 → 4 intentos antes de rendirse
        assert route.call_count == 4


@pytest.mark.asyncio
async def test_unexpected_shape_raises() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json={"type": "message"})
        async with _client() as c:
            with pytest.raises(AnthropicError):
                await c.complete([ChatMessage("user", "x")])


@pytest.mark.asyncio
async def test_json_object_normalizes_fenced_output() -> None:
    """JSON por prompt: con response_format=json_object, los fences/prosa se extraen."""
    fenced = 'Claro, acá va:\n```json\n{"same": true}\n```'
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=_ok(fenced))
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")], response_format="json_object")
        assert r.content == '{"same": true}'


@pytest.mark.asyncio
async def test_text_format_passes_fences_through() -> None:
    """Sin json_object NO se sanea: el summarizer consume texto tal cual."""
    fenced = '```json\n{"same": true}\n```'
    with respx.mock(base_url=BASE_URL) as router:
        router.post(MESSAGES).respond(json=_ok(fenced))
        async with _client() as c:
            r = await c.complete([ChatMessage("user", "x")])
        assert r.content == fenced


def test_anthropic_client_satisfies_llmclient_protocol() -> None:
    assert issubclass(AnthropicClient, LLMClient)


def test_anthropic_error_subclasses_llm_error() -> None:
    assert issubclass(AnthropicError, LLMError)
