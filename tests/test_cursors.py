"""Tests for the typed cursor models (memex.core.cursors)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex.core.cursors import FolderState, ImapCursor


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
