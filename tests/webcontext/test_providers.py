"""Registry + build_provider_from_env: resolve, cadena, descarte sin key, fallback."""

from __future__ import annotations

import shutil

import pytest

from memex.webcontext import WebContextConfigError, build_provider_from_env, known_providers
from memex.webcontext.providers import resolve


def test_resolve_returns_builders() -> None:
    assert resolve("codex") is not None
    assert resolve("firecrawl") is not None


def test_resolve_unknown_keyerror() -> None:
    with pytest.raises(KeyError):
        resolve("bing")


def test_known_providers() -> None:
    assert known_providers() == ["codex", "firecrawl"]


def test_build_codex_only_when_no_firecrawl_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin FIRECRAWL_API_KEY, firecrawl se descarta y queda codex solo (sin envoltorio fallback)."""
    monkeypatch.setattr(shutil, "which", lambda _: "C:/fake/codex.cmd")
    from memex.webcontext.codex import CodexWebContextProvider

    provider = build_provider_from_env({})
    assert isinstance(provider, CodexWebContextProvider)


def test_build_fallback_chain_when_both_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "C:/fake/codex.cmd")
    from memex.webcontext.fallback import FallbackWebContextProvider

    provider = build_provider_from_env({"FIRECRAWL_API_KEY": "fc-x"})
    assert isinstance(provider, FallbackWebContextProvider)


def test_build_firecrawl_only_via_arg() -> None:
    from memex.webcontext.firecrawl import FirecrawlProvider

    provider = build_provider_from_env({"FIRECRAWL_API_KEY": "fc-x"}, provider="firecrawl")
    assert isinstance(provider, FirecrawlProvider)


def test_build_firecrawl_only_missing_key_raises() -> None:
    with pytest.raises(WebContextConfigError):
        build_provider_from_env({}, provider="firecrawl")
