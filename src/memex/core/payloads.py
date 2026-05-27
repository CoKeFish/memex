"""Typed payload models for SourceRecord.

Every ingestor must produce a payload that is a subclass of `BasePayload`.
Pydantic validates the shape at construction time; the parser-level code can
no longer write `payload["form"]` instead of `payload["from"]` — that mistake
becomes a static type error at the field-access site, not a downstream KeyError.

At the wire/DB boundary, payloads serialize to JSON via `.model_dump(mode="json",
by_alias=True)`. The DB column stays JSONB (flexible per source type); the
typing discipline lives in the code, not the schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BasePayload(BaseModel):
    """Marker base for any source-specific payload.

    Subclass this for every new source type (EmailPayload, TelegramPayload, ...).
    SourceRecord only accepts subclasses of BasePayload, which forces the parser
    to construct a validated object instead of a free-form dict.
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",  # surface unknown fields as errors during dev
    )


class Address(BaseModel):
    """RFC 5322 address: email + optional display name."""

    email: str
    name: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class Attachment(BaseModel):
    """Attachment metadata only — content is never carried."""

    filename: str | None
    content_type: str
    size: int
    content_id: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class EmailPayload(BasePayload):
    """Payload schema for ingested email messages.

    The `from` Python keyword is aliased to `from_`; serialization uses the
    alias so JSON keeps the original header name. Optional fields default to
    None / empty list; required fields (`date`, `folder`) must be supplied.
    """

    from_: Address | None = Field(default=None, alias="from")
    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    reply_to: list[Address] = Field(default_factory=list)

    subject: str | None = None
    date: datetime
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)

    list_id: str | None = None
    list_unsubscribe: str | None = None
    list_unsubscribe_post: str | None = None
    precedence: str | None = None
    auto_submitted: str | None = None

    body_text: str = ""
    body_source: Literal["text", "html_stripped"] = "text"
    body_truncated: bool = False

    folder: str
    flags: list[str] = Field(default_factory=list)
    size_bytes: int = 0

    attachments: list[Attachment] = Field(default_factory=list)
    raw_headers: dict[str, str] = Field(default_factory=dict)
