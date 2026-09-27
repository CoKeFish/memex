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


def test_pdf_caps_defaults() -> None:
    caps = OcrConfig.from_env({"OCR_API_KEY": "k"}).pdf_caps()
    assert (caps.max_images, caps.max_pages, caps.min_image_px, caps.text_min_chars) == (
        5,
        5,
        200,
        32,
    )
    assert caps.raster_dpi == 150  # default de PdfCaps (no es env var aún)


def test_pdf_caps_from_env_override() -> None:
    caps = OcrConfig.from_env(
        {
            "OCR_API_KEY": "k",
            "MEMEX_OCR_PDF_MAX_IMAGES": "12",
            "MEMEX_OCR_PDF_MAX_PAGES": "8",
            "MEMEX_OCR_PDF_MIN_IMAGE_PX": "150",
            "MEMEX_OCR_PDF_TEXT_MIN_CHARS": "64",
        }
    ).pdf_caps()
    assert (caps.max_images, caps.max_pages, caps.min_image_px, caps.text_min_chars) == (
        12,
        8,
        150,
        64,
    )


def test_pdf_caps_invalid_int_raises() -> None:
    with pytest.raises(OcrConfigError):
        OcrConfig.from_env({"OCR_API_KEY": "k", "MEMEX_OCR_PDF_MAX_IMAGES": "abc"})


def test_pdf_caps_non_positive_raises() -> None:
    with pytest.raises(OcrConfigError):
        OcrConfig.from_env({"OCR_API_KEY": "k", "MEMEX_OCR_PDF_MAX_PAGES": "0"})


def test_zip_caps_defaults() -> None:
    caps = OcrConfig.from_env({"OCR_API_KEY": "k"}).zip_caps()
    assert caps.max_entries == 20
    assert caps.max_total_bytes == 50 * 1024 * 1024
    assert caps.max_entry_bytes == 15 * 1024 * 1024


def test_zip_caps_from_env_mb_to_bytes() -> None:
    caps = OcrConfig.from_env(
        {
            "OCR_API_KEY": "k",
            "MEMEX_OCR_ZIP_MAX_ENTRIES": "8",
            "MEMEX_OCR_ZIP_MAX_TOTAL_MB": "30",
            "MEMEX_OCR_ZIP_MAX_ENTRY_MB": "5",
        }
    ).zip_caps()
    assert caps.max_entries == 8
    assert caps.max_total_bytes == 30 * 1024 * 1024
    assert caps.max_entry_bytes == 5 * 1024 * 1024


def test_password_pool_empty_by_default() -> None:
    cfg = OcrConfig.from_env({"OCR_API_KEY": "k"})
    assert cfg.password_pool() == ()


def test_password_pool_parsed_and_redacted() -> None:
    cfg = OcrConfig.from_env({"OCR_API_KEY": "k", "MEMEX_ATTACHMENT_PASSWORDS": "docID42, otra , "})
    assert cfg.password_pool() == ("docID42", "otra")  # trim + descarta vacíos
    assert "docID42" not in repr(cfg)  # SecretStr → redactado en repr/logs
