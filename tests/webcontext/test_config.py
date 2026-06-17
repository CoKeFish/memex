"""WebContextConfig.from_env + resolve_chain: cadena, descarte sin key, redacción del secreto."""

from __future__ import annotations

import pytest

from memex.webcontext import (
    WebContextConfig,
    WebContextConfigError,
    known_providers,
    resolve_chain,
)


def test_default_chain_codex_then_firecrawl() -> None:
    assert resolve_chain({}) == ["codex", "firecrawl"]


def test_chain_env_override_comma() -> None:
    assert resolve_chain({"MEMEX_WEBCONTEXT_PROVIDER": "firecrawl,codex"}) == ["firecrawl", "codex"]


def test_chain_arg_forces_single() -> None:
    assert resolve_chain({"MEMEX_WEBCONTEXT_PROVIDER": "firecrawl,codex"}, provider="codex") == [
        "codex"
    ]


def test_chain_unknown_raises() -> None:
    with pytest.raises(WebContextConfigError):
        resolve_chain({"MEMEX_WEBCONTEXT_PROVIDER": "bing"})


def test_from_env_codex_no_key() -> None:
    cfg = WebContextConfig.from_env({}, provider="codex")
    assert cfg.provider == "codex"
    assert cfg.api_key is None
    assert cfg.base_url is None


def test_from_env_firecrawl_key() -> None:
    cfg = WebContextConfig.from_env({"FIRECRAWL_API_KEY": "fc-x"}, provider="firecrawl")
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == "fc-x"
    assert cfg.api_key_env == "FIRECRAWL_API_KEY"
    assert cfg.base_url == "https://api.firecrawl.dev"


def test_from_env_firecrawl_missing_key_raises() -> None:
    with pytest.raises(WebContextConfigError):
        WebContextConfig.from_env({}, provider="firecrawl")


def test_from_env_unknown_provider_raises() -> None:
    with pytest.raises(WebContextConfigError):
        WebContextConfig.from_env({}, provider="bing")


def test_base_url_override_arg() -> None:
    cfg = WebContextConfig.from_env(
        {"FIRECRAWL_API_KEY": "x"}, provider="firecrawl", base_url="http://proxy"
    )
    assert cfg.base_url == "http://proxy"


def test_secret_redacted_in_repr_and_str() -> None:
    cfg = WebContextConfig.from_env({"FIRECRAWL_API_KEY": "SUPERSECRET"}, provider="firecrawl")
    assert "SUPERSECRET" not in repr(cfg)
    assert "SUPERSECRET" not in str(cfg)


def test_known_providers() -> None:
    assert known_providers() == ["codex", "firecrawl"]
