"""Corta-circuito por proveedor: abre tras dos topes seguidos, cierra con una respuesta buena."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from memex.llm import breaker
from memex.llm.client import ChatMessage, LLMDeadlineError
from memex.llm.config import LLMConfig
from memex.llm.deepseek import DeepSeekClient

BASE_URL = "https://api.deepseek.com"
CHAT = "/chat/completions"


def _cfg(**extra: Any) -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr("TKN"),
        base_url=BASE_URL,
        default_model="deepseek-chat",
        deadline_s=0.05,
        **extra,
    )


def _ok_body() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_una_falla_no_abre_dos_si() -> None:
    k = "deepseek:https://api.deepseek.com"
    assert breaker.record_failure(k) == 0.0  # la primera puede ser mala suerte
    assert breaker.seconds_left(k) == 0.0
    assert breaker.record_failure(k) > 0.0
    assert breaker.seconds_left(k) > 0.0


def test_una_respuesta_buena_borra_la_racha() -> None:
    k = "deepseek:x"
    breaker.record_failure(k)
    breaker.record_success(k)
    assert breaker.record_failure(k) == 0.0  # la racha arrancó de cero otra vez


@pytest.mark.asyncio
async def test_tras_dos_topes_la_tercera_llamada_ni_sale_a_la_red() -> None:
    """El caso real: con el proveedor caído, seguir esperando 180 s por mensaje es la mitad del
    problema. A la tercera ya no se le pregunta: la cadena de fallback pasa directo al siguiente."""
    import asyncio

    intentos = 0

    async def nunca(_request: httpx.Request) -> httpx.Response:
        nonlocal intentos
        intentos += 1
        await asyncio.sleep(3600)
        raise AssertionError("inalcanzable")

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.post(CHAT).mock(side_effect=nunca)
        async with DeepSeekClient(_cfg()) as c:
            for _ in range(2):
                with pytest.raises(LLMDeadlineError):
                    await c.complete([ChatMessage("user", "x")])
            assert intentos == 2
            with pytest.raises(LLMDeadlineError) as exc:
                await c.complete([ChatMessage("user", "x")])
    assert intentos == 2, "la tercera no tocó la red"
    assert "cortado" in str(exc.value)
    assert exc.value.status_code == 0  # sigue cayendo al siguiente proveedor


@pytest.mark.asyncio
async def test_el_corte_es_por_proveedor_no_global() -> None:
    """Un proveedor caído no puede apagar a otro que responde bien."""
    breaker.record_failure("deepseek:otra-url")
    breaker.record_failure("deepseek:otra-url")
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CHAT).respond(200, json=_ok_body())
        async with DeepSeekClient(_cfg()) as c:
            r = await c.complete([ChatMessage("user", "x")])
    assert r.content == "ok"


@pytest.mark.asyncio
async def test_responder_bien_reabre_al_proveedor() -> None:
    import asyncio

    async def nunca(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("inalcanzable")

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        route = router.post(CHAT).mock(side_effect=nunca)
        async with DeepSeekClient(_cfg()) as c:
            with pytest.raises(LLMDeadlineError):
                await c.complete([ChatMessage("user", "x")])
            route.mock(return_value=httpx.Response(200, json=_ok_body()))
            assert (await c.complete([ChatMessage("user", "x")])).content == "ok"
            # la buena borró la racha: la siguiente falla sola no alcanza para abrir
            route.mock(side_effect=nunca)
            with pytest.raises(LLMDeadlineError):
                await c.complete([ChatMessage("user", "x")])
            assert breaker.seconds_left(f"deepseek:{BASE_URL}") == 0.0
