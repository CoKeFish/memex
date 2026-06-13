"""Resolución de `llm_consumer_settings` (get/upsert): fila propia → default → hardcode DeepSeek."""

from __future__ import annotations

from typing import Any

import pytest

from memex.llm.settings import (
    LLMConsumerSettings,
    get_consumer_settings,
    list_consumer_settings,
    upsert_consumer_settings,
)


def test_no_row_falls_back_to_hardcoded_deepseek(conn: Any) -> None:
    s = get_consumer_settings(conn, 1, "summarizer")
    assert s == LLMConsumerSettings()
    assert s.provider == "deepseek" and s.model is None and s.fallback == ()


def test_default_row_used_when_consumer_absent(conn: Any) -> None:
    upsert_consumer_settings(conn, 1, "default", provider="anthropic", model="claude-opus-4-8")
    s = get_consumer_settings(conn, 1, "summarizer")  # sin fila propia → cae al default
    assert s.provider == "anthropic" and s.model == "claude-opus-4-8"


def test_consumer_row_wins_over_default(conn: Any) -> None:
    upsert_consumer_settings(conn, 1, "default", provider="anthropic")
    upsert_consumer_settings(conn, 1, "summarizer", provider="codex", codex_model="gpt-5.1")
    s = get_consumer_settings(conn, 1, "summarizer")
    assert s.provider == "codex" and s.codex_model == "gpt-5.1"
    # otro consumer sin fila propia sigue cayendo al default
    assert get_consumer_settings(conn, 1, "orchestrator").provider == "anthropic"


def test_upsert_is_partial(conn: Any) -> None:
    upsert_consumer_settings(conn, 1, "orchestrator", provider="anthropic", model="claude-opus-4-8")
    # tocar solo el modelo NO cambia el provider
    s = upsert_consumer_settings(conn, 1, "orchestrator", model="claude-sonnet-4-6")
    assert s.provider == "anthropic" and s.model == "claude-sonnet-4-6"


def test_empty_string_clears_model_override(conn: Any) -> None:
    upsert_consumer_settings(conn, 1, "orchestrator", provider="deepseek", model="deepseek-v4-pro")
    s = upsert_consumer_settings(conn, 1, "orchestrator", model="")
    assert s.model is None  # vuelve al default del proveedor


def test_fallback_chain_roundtrips(conn: Any) -> None:
    s = upsert_consumer_settings(
        conn, 1, "orchestrator", provider="codex", fallback=["deepseek", "anthropic"]
    )
    assert s.fallback == ("deepseek", "anthropic")
    assert get_consumer_settings(conn, 1, "orchestrator").fallback == ("deepseek", "anthropic")
    # fallback=[] borra la cadena
    assert upsert_consumer_settings(conn, 1, "orchestrator", fallback=[]).fallback == ()


def test_list_only_configured_rows(conn: Any) -> None:
    upsert_consumer_settings(conn, 1, "summarizer", provider="codex")
    upsert_consumer_settings(conn, 1, "quality_judge", provider="anthropic")
    rows = list_consumer_settings(conn, 1)
    assert set(rows) == {"summarizer", "quality_judge"}


@pytest.mark.parametrize(
    ("kwargs", "marker"),
    [
        ({"consumer": "no-existe", "provider": "deepseek"}, "consumer"),
        ({"consumer": "summarizer", "provider": "openai"}, "provider"),
        ({"consumer": "summarizer", "fallback": ["deepseek", "nope"]}, "fallback"),
    ],
)
def test_validation_rejects_bad_input(conn: Any, kwargs: dict[str, Any], marker: str) -> None:
    consumer = kwargs.pop("consumer")
    with pytest.raises(ValueError, match=marker):
        upsert_consumer_settings(conn, 1, consumer, **kwargs)
