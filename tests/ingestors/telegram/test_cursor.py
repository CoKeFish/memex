"""TelegramCursor + ChatCursor — shape + roundtrip."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.core.cursors import ChatCursor, TelegramCursor


def test_default_telegram_cursor_is_empty() -> None:
    c = TelegramCursor()
    assert c.chats == {}


def test_chat_cursor_default_last_message_id() -> None:
    cc = ChatCursor()
    assert cc.last_message_id == 0


def test_telegram_cursor_roundtrips_via_dict() -> None:
    cc = ChatCursor(last_message_id=42)
    c = TelegramCursor(chats={"-1001234567890": cc, "-100777": ChatCursor(last_message_id=7)})
    blob = c.model_dump(mode="json")
    c2 = TelegramCursor.model_validate(blob)
    assert c2 == c


def test_chat_cursor_is_frozen() -> None:
    cc = ChatCursor(last_message_id=42)
    with pytest.raises(ValidationError):
        cc.last_message_id = 99  # type: ignore[misc]


def test_telegram_cursor_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TelegramCursor.model_validate({"chats": {}, "bogus": True})


def test_chat_cursor_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChatCursor.model_validate({"last_message_id": 1, "bogus": True})
