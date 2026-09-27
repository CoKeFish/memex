"""Fábrica `build_llm_client` / `build_provider_client` / `aclose_llm`.

La lógica de cadena se prueba con `_build_one` + `get_consumer_settings` monkeypatcheados (sin
DB ni claves reales); la construcción concreta de un proveedor se prueba con claves dummy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from memex.llm import registry
from memex.llm.anthropic import AnthropicClient
from memex.llm.codex import CodexError
from memex.llm.config import LLMConfigError
from memex.llm.deepseek import DeepSeekClient
from memex.llm.fallback import FallbackClient
from memex.llm.settings import LLMConsumerSettings


class _Fake:
    """Cliente sentinela: marca su proveedor, no se invoca."""

    def __init__(self, provider: str) -> None:
        self.provider = provider


def _patch(
    monkeypatch: pytest.MonkeyPatch, settings: LLMConsumerSettings, *, codex_ok: bool = True
) -> None:
    monkeypatch.setattr(registry, "get_consumer_settings", lambda conn, uid, c: settings)

    def fake_build_one(provider: str, s: LLMConsumerSettings) -> Any:
        if provider == "codex" and not codex_ok:
            raise CodexError(0, "sin binario/sesión")
        return _Fake(provider)

    monkeypatch.setattr(registry, "_build_one", fake_build_one)


def test_single_provider_returns_bare_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, LLMConsumerSettings(provider="deepseek"))
    client = registry.build_llm_client("summarizer")
    assert isinstance(client, _Fake) and client.provider == "deepseek"


def test_chain_wraps_in_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, LLMConsumerSettings(provider="deepseek", fallback=("anthropic",)))
    client = registry.build_llm_client("orchestrator")
    assert isinstance(client, FallbackClient)
    assert [name for name, _ in client._clients] == ["deepseek", "anthropic"]


def test_codex_skipped_when_unavailable_but_others_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        LLMConsumerSettings(provider="codex", fallback=("deepseek",)),
        codex_ok=False,
    )
    client = registry.build_llm_client("orchestrator")
    # codex se omite; queda DeepSeek solo → cliente directo (no FallbackClient)
    assert isinstance(client, _Fake) and client.provider == "deepseek"


def test_codex_skipped_keeps_remaining_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        LLMConsumerSettings(provider="deepseek", fallback=("codex", "anthropic")),
        codex_ok=False,
    )
    client = registry.build_llm_client("orchestrator")
    assert isinstance(client, FallbackClient)
    assert [name for name, _ in client._clients] == ["deepseek", "anthropic"]


def test_codex_only_and_unavailable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, LLMConsumerSettings(provider="codex"), codex_ok=False)
    with pytest.raises(CodexError):
        registry.build_llm_client("orchestrator")


def test_all_unavailable_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # cadena de >1 codex, todos ausentes → built vacío → LLMConfigError
    _patch(monkeypatch, LLMConsumerSettings(provider="codex", fallback=("codex",)), codex_ok=False)
    with pytest.raises(LLMConfigError):
        registry.build_llm_client("orchestrator")


def test_build_provider_client_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy")
    client = registry.build_provider_client("deepseek", model="deepseek-v4-pro")
    assert isinstance(client, DeepSeekClient)
    assert client._config.default_model == "deepseek-v4-pro"


def test_build_provider_client_anthropic_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = registry.build_provider_client("anthropic", model="claude-sonnet-4-6")
    assert isinstance(client, AnthropicClient)
    assert client._config.default_model == "claude-sonnet-4-6"


def test_aclose_llm_closes_when_present() -> None:
    closed = {"v": False}

    class _WithClose:
        async def aclose(self) -> None:
            closed["v"] = True

    asyncio.run(registry.aclose_llm(_WithClose()))  # type: ignore[arg-type]
    assert closed["v"] is True


def test_aclose_llm_noop_without_aclose() -> None:
    asyncio.run(registry.aclose_llm(_Fake("codex")))  # type: ignore[arg-type]  # no debe explotar
