"""Tests for the typed payload models (memex.core.payloads).

The point of these tests is to verify the discipline: catch malformed payload
construction at the parser's call site, not later as KeyErrors downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memex.core.payloads import Address, Attachment, EmailPayload


def test_email_payload_minimal_valid() -> None:
    p = EmailPayload(date=datetime(2026, 5, 26, tzinfo=UTC), folder="INBOX")
    assert p.subject is None
    assert p.from_ is None
    assert p.to == []
    assert p.body_text == ""
    assert p.body_source == "text"


def test_email_payload_full_roundtrip() -> None:
    p = EmailPayload(
        from_=Address(email="alice@example.com", name="Alice"),
        to=[Address(email="bob@example.com", name=None)],
        cc=[],
        reply_to=[],
        subject="Hello",
        date=datetime(2026, 5, 26, 14, 30, tzinfo=UTC),
        message_id="abc123@example.com",
        body_text="hi",
        body_source="text",
        folder="INBOX",
        flags=["\\Seen"],
        size_bytes=512,
    )
    dumped = p.model_dump(mode="json", by_alias=True)
    assert dumped["from"] == {"email": "alice@example.com", "name": "Alice"}
    assert dumped["to"] == [{"email": "bob@example.com", "name": None}]
    assert dumped["subject"] == "Hello"
    assert dumped["message_id"] == "abc123@example.com"
    assert "from_" not in dumped, "alias 'from' must be used in serialization"


def test_email_payload_rejects_unknown_fields() -> None:
    """`extra='forbid'` catches typos at construction time."""
    with pytest.raises(ValidationError) as exc:
        EmailPayload(
            date=datetime(2026, 5, 26, tzinfo=UTC),
            folder="INBOX",
            sender="alice@example.com",  # type: ignore[call-arg]  # typo: should be from_
        )
    assert "sender" in str(exc.value)


def test_email_payload_rejects_invalid_body_source() -> None:
    """Literal type enforces allowed values."""
    with pytest.raises(ValidationError):
        EmailPayload(
            date=datetime(2026, 5, 26, tzinfo=UTC),
            folder="INBOX",
            body_source="markdown",
        )


def test_email_payload_requires_folder() -> None:
    with pytest.raises(ValidationError) as exc:
        EmailPayload(date=datetime(2026, 5, 26, tzinfo=UTC))  # type: ignore[call-arg]
    assert "folder" in str(exc.value)


def test_email_payload_frozen() -> None:
    p = EmailPayload(date=datetime(2026, 5, 26, tzinfo=UTC), folder="INBOX")
    with pytest.raises(ValidationError):
        p.subject = "mutated"  # type: ignore[misc]


def test_address_minimal() -> None:
    addr = Address(email="x@y.com")
    assert addr.email == "x@y.com"
    assert addr.name is None


def test_attachment_required_fields() -> None:
    att = Attachment(filename="report.pdf", content_type="application/pdf", size=1024)
    assert att.content_id is None
    with pytest.raises(ValidationError):
        Attachment(content_type="application/pdf", size=0)  # type: ignore[call-arg]
