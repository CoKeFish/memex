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


class TelegramSender(BaseModel):
    """Quien envió un mensaje de Telegram.

    `None` para service messages o canales con autoría anónima (broadcast posts
    sin sender concreto). `display_name` se arma de `first_name + last_name` o
    `title` según el tipo de peer.
    """

    user_id: int
    username: str | None = None
    display_name: str | None = None
    is_bot: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid")


class TelegramPayload(BasePayload):
    """Payload schema para mensajes de Telegram ingestados (grupos / supergrupos / canales).

    DMs (chats privados con un usuario) NO se persisten — el parser las rechaza
    explícitamente antes de construir este payload. Si llegás a ver un payload
    con `chat_kind="dm"`, es un bug del parser.

    `chat_id` viene normalizado al "marked format" estable de Telethon vía
    `telethon.utils.get_peer_id`: `-(1e12 + id)` para Channel/supergroup,
    `-<id>` para grupos básicos, `<id>` para usuarios. Ese id marcado se
    puede pasar a `iter_messages`/`get_entity` y Telethon lo acepta;
    persistir SIN normalizar lleva a mismatches silenciosos vs allowlist.

    `topic_id` aplica solo a foros (supergrupos con topics habilitados); usa
    el `reply_to_top_id` de Telethon (el root del topic), NO el
    `reply_to_msg_id` (que es el mensaje al que respondés dentro del topic).
    """

    chat_id: int
    chat_kind: Literal["group", "supergroup", "channel"]
    chat_title: str | None = None
    topic_id: int | None = None

    message_id: int
    sender: TelegramSender | None = None
    date: datetime

    text: str = ""

    reply_to_message_id: int | None = None
    forwarded_from: str | None = None

    media_kind: Literal[
        "none", "photo", "video", "document", "audio", "voice", "sticker", "other"
    ] = "none"
    media_caption: str | None = None


class SocialEngagement(BaseModel):
    """Métricas de engagement de un post social. Todas opcionales — no toda
    plataforma expone todas, y un scraper puede no devolverlas.

    Convención de mapeo entre plataformas: en X (Twitter) `shares`=retweets y
    `comments`=replies; en Facebook `shares`=shares; en Instagram `shares` suele
    no existir (queda `None`).
    """

    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class SocialPostPayload(BasePayload):
    """Payload schema para un post público de red social (broadcast).

    Compartido por las tres sources sociales (`instagram`, `facebook`, `x`),
    discriminadas por `platform`. El downstream (classifier / summarizer de
    oportunidades) es agnóstico de plataforma: le importan `text`, `url` y
    `posted_at`; los campos específicos (`shortcode`, `is_paid_partnership`) son
    opcionales y quedan `None` donde no aplican.

    `account` es SIEMPRE el identificador de la allowlist (el handle / página que
    se pidió scrapear), NUNCA el owner scrapeado (ej. el `pageId` numérico de
    Facebook). El `external_id` y el cursor quedan keyed por ese mismo string,
    así `fetch` puede buscar el checkpoint y `advance_checkpoint` recuperarlo del
    `external_id` sin mirar el payload. Si el parser usara el owner scrapeado, la
    key no matchearía y el checkpoint nunca persistiría.
    """

    platform: Literal["instagram", "facebook", "x"]
    account: str
    account_name: str | None = None

    post_id: str
    shortcode: str | None = None
    url: str
    text: str = ""
    posted_at: datetime

    media_kind: Literal["none", "image", "video", "carousel", "reel", "other"] = "none"
    engagement: SocialEngagement | None = None
    is_paid_partnership: bool | None = None
    raw_type: str | None = None
