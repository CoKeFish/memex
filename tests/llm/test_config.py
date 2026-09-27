"""LLMConfig.from_env — resolución de DEEPSEEK_API_KEY, redacción, jerarquía de error."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from memex.llm.client import LLMError
from memex.llm.config import _DEFAULT_API_KEY_ENV, LLMConfig, LLMConfigError


def test_from_env_resolves_key_and_defaults() -> None:
    cfg = LLMConfig.from_env({"DEEPSEEK_API_KEY": "sk-xyz"})
    assert cfg.api_key.get_secret_value() == "sk-xyz"
    assert cfg.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.default_model == "deepseek-chat"


def test_from_env_missing_var_raises() -> None:
    with pytest.raises(LLMConfigError):
        LLMConfig.from_env({})


def test_from_env_empty_value_raises() -> None:
    with pytest.raises(LLMConfigError):
        LLMConfig.from_env({"DEEPSEEK_API_KEY": "   "})


def test_from_env_custom_var_name() -> None:
    cfg = LLMConfig.from_env({"MY_KEY": "v"}, api_key_env="MY_KEY")
    assert cfg.api_key.get_secret_value() == "v"
    assert cfg.api_key_env == "MY_KEY"


def test_from_env_overrides_base_url_and_model() -> None:
    cfg = LLMConfig.from_env(
        {"DEEPSEEK_API_KEY": "k"},
        base_url="https://example.test/",
        default_model="deepseek-v4-pro",
    )
    assert cfg.base_url == "https://example.test/"
    assert cfg.default_model == "deepseek-v4-pro"


def test_secret_not_leaked_in_repr_or_dump() -> None:
    cfg = LLMConfig(api_key=SecretStr("super-secret"))
    assert "super-secret" not in repr(cfg)
    assert "super-secret" not in str(cfg.model_dump())


def test_config_error_subclasses_llm_error() -> None:
    assert issubclass(LLMConfigError, LLMError)


def test_default_api_key_env_is_canonical_deepseek_name() -> None:
    assert _DEFAULT_API_KEY_ENV == "DEEPSEEK_API_KEY"
