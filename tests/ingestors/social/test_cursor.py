"""SocialCursor + AccountCursor — shape + roundtrip."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memex.core.cursors import AccountCursor, SocialCursor


def test_default_social_cursor_is_empty() -> None:
    c = SocialCursor()
    assert c.accounts == {}


def test_account_cursor_defaults() -> None:
    ac = AccountCursor()
    assert ac.last_post_id == ""
    assert ac.last_posted_at is None


def test_social_cursor_roundtrips_via_dict() -> None:
    ac = AccountCursor(last_post_id="abc", last_posted_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC))
    c = SocialCursor(accounts={"utn.frba": ac, "fiuba": AccountCursor(last_post_id="z")})
    blob = c.model_dump(mode="json")
    c2 = SocialCursor.model_validate(blob)
    assert c2 == c


def test_account_cursor_is_frozen() -> None:
    ac = AccountCursor(last_post_id="abc")
    with pytest.raises(ValidationError):
        ac.last_post_id = "zzz"  # type: ignore[misc]


def test_social_cursor_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SocialCursor.model_validate({"accounts": {}, "bogus": True})


def test_account_cursor_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AccountCursor.model_validate({"last_post_id": "a", "bogus": True})
