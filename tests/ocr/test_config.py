"""OcrConfig.from_env: resolución de key (con fallback OpenAI), base_url y modelo."""

from __future__ import annotations

import pytest

from memex.ocr.config import OcrConfig, OcrConfigError


def test_reads_ocr_api_key() -> None:
    cfg = OcrConfig.from_env({"OCR_API_KEY": "sk-ocr"})
    assert cfg.api_key.get_secret_value() == "sk-ocr"
    assert cfg.api_key_env == "OCR_API_KEY"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.default_model == "gpt-4o-mini"


def test_falls_back_to_openai_api_key() -> None:
    cfg = OcrConfig.from_env({"OPENAI_API_KEY": "sk-openai"})
    assert cfg.api_key.get_secret_value() == "sk-openai"
    assert cfg.api_key_env == "OPENAI_API_KEY"  # registra cuál se usó


def test_ocr_key_wins_over_fallback() -> None:
    cfg = OcrConfig.from_env({"OCR_API_KEY": "sk-ocr", "OPENAI_API_KEY": "sk-openai"})
    assert cfg.api_key.get_secret_value() == "sk-ocr"


def test_base_url_and_model_from_env() -> None:
    cfg = OcrConfig.from_env(
        {
            "OCR_API_KEY": "k",
            "MEMEX_OCR_BASE_URL": "https://vision.example.com/v1",
            "MEMEX_OCR_MODEL": "qwen-vl",
        }
    )
    assert cfg.base_url == "https://vision.example.com/v1"
    assert cfg.default_model == "qwen-vl"


def test_explicit_model_arg_overrides_env() -> None:
    cfg = OcrConfig.from_env({"OCR_API_KEY": "k", "MEMEX_OCR_MODEL": "from-env"}, model="from-arg")
    assert cfg.default_model == "from-arg"


def test_raises_when_no_key() -> None:
    with pytest.raises(OcrConfigError):
        OcrConfig.from_env({})
