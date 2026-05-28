"""TelegramConfig.from_source_config — env var resolution + validation."""

from __future__ import annotations

import pytest

from memex.core.source import SourceConfigError
from memex.ingestors.telegram.config import (
    AllowedChat,
    TelegramConfig,
    TelegramConfigError,
)

VALID_ENV = {
    "MEMEX_TG_API_ID": "12345",
    "MEMEX_TG_API_HASH": "deadbeefcafebabe",
    "MEMEX_TG_PHONE": "+34999888777",
}


def test_minimal_config_resolves_defaults() -> None:
    cfg = TelegramConfig.from_source_config({}, env=VALID_ENV)
    assert cfg.api_id == 12345
    assert cfg.api_hash == "deadbeefcafebabe"
    assert cfg.phone == "+34999888777"
    assert cfg.session_name == "default"
    # session_path is canonicalized via Path.resolve() — exact form is platform-
    # dependent (Windows vs POSIX) but it must be absolute.
    assert cfg.session_path.is_absolute()
    assert cfg.allowed_chats == []
    assert cfg.batch_size == 100


def test_allowed_chats_parsed_into_objects() -> None:
    cfg = TelegramConfig.from_source_config(
        {
            "allowed_chats": [
                {"chat_id": -100123},
                {"chat_id": -100456, "topic_ids": [1, 2], "streaming": True, "priority": True},
            ]
        },
        env=VALID_ENV,
    )
    assert len(cfg.allowed_chats) == 2
    assert cfg.allowed_chats[0] == AllowedChat(chat_id=-100123)
    assert cfg.allowed_chats[1] == AllowedChat(
        chat_id=-100456, topic_ids=[1, 2], streaming=True, priority=True
    )


def test_missing_env_var_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config({}, env={})


def test_non_integer_api_id_raises() -> None:
    bad_env = {**VALID_ENV, "MEMEX_TG_API_ID": "not-a-number"}
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config({}, env=bad_env)


def test_config_error_is_source_config_error() -> None:
    """Generic except SourceConfigError catches Telegram-specific errors."""
    assert issubclass(TelegramConfigError, SourceConfigError)


def test_repr_redacts_api_hash_and_phone() -> None:
    cfg = TelegramConfig.from_source_config({}, env=VALID_ENV)
    r = repr(cfg)
    assert "deadbeefcafebabe" not in r
    assert "+34999888777" not in r
    assert "<redacted>" in r
    assert "phone_masked" not in r  # we use property in __repr__ not its name
    # phone_masked is "+34***77"
    assert "+34" in r


def test_session_file_property() -> None:
    cfg = TelegramConfig.from_source_config(
        {"session_path": "/tmp/x", "session_name": "myaccount"},
        env=VALID_ENV,
    )
    # session_file = session_path / session_name; session_path is canonicalized.
    assert cfg.session_file == cfg.session_path / "myaccount"
    assert cfg.session_file.name == "myaccount"


def test_session_path_with_double_dot_raises() -> None:
    """Path traversal protection: raw '..' in session_path is rejected."""
    with pytest.raises(TelegramConfigError, match="path traversal"):
        TelegramConfig.from_source_config(
            {"session_path": "/var/lib/memex/../etc"},
            env=VALID_ENV,
        )


def test_session_name_with_separator_raises() -> None:
    """Path traversal protection: session_name must match strict regex."""
    for bad in ("../etc", "foo/bar", "foo.bar", "foo bar", ""):
        with pytest.raises(TelegramConfigError):
            TelegramConfig.from_source_config({"session_name": bad}, env=VALID_ENV)


def test_invalid_allowed_chats_shape_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config({"allowed_chats": "not-a-list"}, env=VALID_ENV)
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config(
            {"allowed_chats": [{"chat_id": -100, "bogus": "x"}]}, env=VALID_ENV
        )


def test_zero_or_negative_batch_size_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config({"batch_size": 0}, env=VALID_ENV)


def test_empty_session_name_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramConfig.from_source_config({"session_name": "  "}, env=VALID_ENV)


def test_allowed_chat_matches_topic_when_topic_ids_none() -> None:
    """topic_ids=None means: accept any topic AND the top-level (no topic)."""
    ac = AllowedChat(chat_id=-100, topic_ids=None)
    assert ac.matches_topic(None) is True
    assert ac.matches_topic(5) is True
    assert ac.matches_topic(99) is True


def test_allowed_chat_matches_topic_when_topic_ids_empty() -> None:
    """topic_ids=[] means: only accept top-level (no topic)."""
    ac = AllowedChat(chat_id=-100, topic_ids=[])
    assert ac.matches_topic(None) is True
    assert ac.matches_topic(5) is False


def test_allowed_chat_matches_topic_when_topic_ids_listed() -> None:
    ac = AllowedChat(chat_id=-100, topic_ids=[1, 2])
    assert ac.matches_topic(1) is True
    assert ac.matches_topic(2) is True
    assert ac.matches_topic(3) is False
    # top-level (no topic) NOT accepted when an explicit list is given
    assert ac.matches_topic(None) is False
