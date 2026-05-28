"""TelegramPayload + TelegramSender — shape, immutability, defaults."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from memex.core.payloads import TelegramPayload, TelegramSender


def _base_kwargs() -> dict[str, Any]:
    return {
        "chat_id": -1001234567890,
        "chat_kind": "supergroup",
        "message_id": 42,
        "date": datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
    }


def test_minimal_payload_has_defaults() -> None:
    p = TelegramPayload(**_base_kwargs())
    assert p.chat_id == -1001234567890
    assert p.chat_kind == "supergroup"
    assert p.message_id == 42
    assert p.sender is None
    assert p.text == ""
    assert p.topic_id is None
    assert p.reply_to_message_id is None
    assert p.forwarded_from is None
    assert p.media_kind == "none"
    assert p.media_caption is None
    assert p.chat_title is None


def test_full_payload_roundtrips_via_json() -> None:
    sender = TelegramSender(user_id=1, username="alice", display_name="Alice", is_bot=False)
    p = TelegramPayload(
        chat_id=-100123,
        chat_kind="channel",
        chat_title="news",
        topic_id=99,
        message_id=42,
        sender=sender,
        date=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        text="hi",
        reply_to_message_id=41,
        forwarded_from="peer:7",
        media_kind="photo",
        media_caption="caption",
    )
    blob = p.model_dump(mode="json", by_alias=True)
    p2 = TelegramPayload.model_validate(blob)
    assert p2 == p


def test_payload_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TelegramPayload.model_validate({**_base_kwargs(), "bogus": "x"})


def test_payload_is_frozen() -> None:
    p = TelegramPayload(**_base_kwargs())
    with pytest.raises(ValidationError):
        p.text = "mutate"  # type: ignore[misc]


def test_payload_rejects_dm_chat_kind() -> None:
    """`dm` is not a valid chat_kind — the parser filters DMs out before
    constructing the payload, so the schema doesn't even allow the literal."""
    with pytest.raises(ValidationError):
        TelegramPayload(**{**_base_kwargs(), "chat_kind": "dm"})


def test_sender_defaults() -> None:
    s = TelegramSender(user_id=7)
    assert s.user_id == 7
    assert s.username is None
    assert s.display_name is None
    assert s.is_bot is False
