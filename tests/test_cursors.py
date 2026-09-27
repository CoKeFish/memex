"""Tests for the typed cursor models (memex.core.cursors)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.core.cursors import FolderState, ImapCursor, summarize_cursor


def test_imap_cursor_empty_default() -> None:
    cursor = ImapCursor()
    assert cursor.folders == {}


def test_imap_cursor_with_folders() -> None:
    cursor = ImapCursor(
        folders={
            "INBOX": FolderState(uidvalidity=17, last_uid=42),
            "Sent": FolderState(uidvalidity=18, last_uid=5),
        }
    )
    assert cursor.folders["INBOX"].last_uid == 42
    assert cursor.folders["Sent"].uidvalidity == 18


def test_imap_cursor_roundtrip_via_dict() -> None:
    """A cursor that came from memex (as dict) must validate back to a typed model."""
    raw = {"folders": {"INBOX": {"uidvalidity": 17, "last_uid": 42}}}
    cursor = ImapCursor.model_validate(raw)
    assert cursor.folders["INBOX"].last_uid == 42

    dumped = cursor.model_dump(mode="json")
    assert dumped == raw


def test_imap_cursor_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ImapCursor.model_validate({"folders": {}, "extra": 1})


def test_folder_state_rejects_missing_field() -> None:
    with pytest.raises(ValidationError):
        FolderState.model_validate({"uidvalidity": 17})


def test_folder_state_frozen() -> None:
    fs = FolderState(uidvalidity=17, last_uid=42)
    with pytest.raises(ValidationError):
        fs.last_uid = 100  # type: ignore[misc]


# ---- summarize_cursor (tooltip del marcador en el timeline de cobertura) ------------------------


def test_summarize_imap_cursor() -> None:
    raw = {
        "folders": {
            "INBOX": {"uidvalidity": 17, "last_uid": 4321},
            "Sent": {"uidvalidity": 18, "last_uid": 99},
        }
    }
    assert summarize_cursor("imap", raw) == "2 carpeta(s) · uid hasta 4321"


def test_summarize_telegram_cursor() -> None:
    raw = {"chats": {"-100123": {"last_message_id": 555}, "-200456": {"last_message_id": 12}}}
    assert summarize_cursor("telegram", raw) == "2 chat(s) · msg hasta 555"


def test_summarize_social_cursor_with_dates() -> None:
    raw = {
        "accounts": {
            "nasa": {"last_post_id": "p9", "last_posted_at": "2026-05-22T10:00:00Z"},
            "spacex": {"last_post_id": "p2", "last_posted_at": "2026-04-01T08:00:00Z"},
        }
    }
    assert summarize_cursor("x", raw) == "2 cuenta(s) · último post 2026-05-22"


def test_summarize_social_cursor_without_dates() -> None:
    raw = {"accounts": {"nasa": {"last_post_id": "", "last_posted_at": None}}}
    assert summarize_cursor("instagram", raw) == "1 cuenta(s)"


def test_summarize_cursor_empty_or_unknown() -> None:
    assert summarize_cursor("imap", {"folders": {}}) == ""
    assert summarize_cursor("dummy", {"whatever": 1}) == ""
    # Cursor malformado → "" (no revienta).
    assert summarize_cursor("imap", {"folders": {"INBOX": {"bogus": True}}}) == ""
