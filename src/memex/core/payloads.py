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


def msgid_dedupe_key(message_id: str | None) -> str | None:
    """Clave de dedup por contenido a partir del Message-ID de un correo.

    Normaliza (quita los ángulos ``<...>`` y espacios) para que el MISMO correo capturado por
    fuentes distintas — IMAP en dos carpetas, IMAP + Outlook, dos cuentas — produzca la MISMA
    clave y se deduplique por contenido. Devuelve ``None`` si no hay Message-ID utilizable.
    La normalización coincide con el `message_id` que ya guardan los parsers (sin ángulos).
    """
    if not message_id:
        return None
    norm = message_id.strip()
    if norm.startswith("<") and norm.endswith(">"):
        norm = norm[1:-1].strip()
    return f"msgid:{norm}" if norm else None


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
    #: True si lo mandó la CUENTA del usuario (Telethon `message.out`): en un grupo, la
    #: perspectiva («me pasó a mí» vs «le pasó a otro») depende de esto.
    is_self: bool = False
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


class WhatsAppSender(BaseModel):
    """Quien escribió un mensaje de WhatsApp: su número (o LID) y el nombre que muestra."""

    phone: str | None = None
    display_name: str | None = None
    #: True si lo mandó la CUENTA del usuario (`IsFromMe`): la perspectiva depende de esto.
    is_self: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid")


class WhatsAppPayload(BasePayload):
    """Mensaje de WhatsApp (grupo o chat 1:1) recibido por el dispositivo vinculado.

    Calca los nombres de `TelegramPayload` (chat_id, chat_title, sender.display_name, text, date,
    media_kind…) para que el render, el contexto de chat y el dashboard lo traten igual; la
    diferencia es que `chat_id` y `message_id` son TEXTO (el JID `123@g.us` y el id del mensaje).
    """

    chat_id: str
    chat_kind: Literal["group", "user"]
    chat_title: str | None = None

    message_id: str
    sender: WhatsAppSender | None = None
    date: datetime

    text: str = ""

    reply_to_message_id: str | None = None
    forwarded: bool = False

    media_kind: Literal[
        "none", "photo", "video", "document", "audio", "voice", "sticker", "other"
    ] = "none"
    media_caption: str | None = None


class NotePayload(BasePayload):
    """Payload de una NOTA del usuario (apps de notas; hoy Notion).

    Una fila = una página/nota en su versión VIGENTE: `external_id` es estable (`notion:<page_id>`)
    y una edición reemplaza el payload (ver `insert_record(on_conflict="replace")`), no crea otra
    fila. `body_text` es el texto de los bloques vuelto markdown llano (encabezados, listas, tareas
    `[x]`, citas, código, tablas); `path` son los títulos de las páginas ancestro hasta la raíz
    configurada (contexto: «Proyectos > memex > Ideas»). Sin remitente: el autor es el usuario.
    `title` la usa el render como encabezado (no hay `subject`).
    """

    provider: Literal["notion"]
    page_id: str
    title: str
    body_text: str = ""
    url: str | None = None
    path: list[str] = Field(default_factory=list)
    root_id: str | None = None
    parent_id: str | None = None
    created_time: datetime
    last_edited_time: datetime
    archived: bool = False
    body_truncated: bool = False
    #: Subpáginas (ids) que cuelgan de esta nota: cada una es su propio registro.
    child_page_ids: list[str] = Field(default_factory=list)


class DocumentPayload(BasePayload):
    """Payload de un DOCUMENTO que el usuario le entrega a memex (extracto bancario, factura,
    contrato): un archivo que él descargó y dejó en una carpeta vigilada.

    A diferencia del correo o el chat, acá el mensaje ES el archivo: el payload solo lo identifica
    y lo ubica; el contenido llega por el OCR del adjunto (capa de texto para un PDF digital) y se
    concatena al render. `body_text` queda para una nota que el usuario quiera dejar junto al
    archivo. Sin remitente: lo aportó el usuario.

    `sha256` es la identidad del archivo (el `external_id` se arma con él): volver a dejar el mismo
    documento no duplica, y renombrarlo tampoco lo vuelve a ingerir. `captured_from` dice de dónde
    salió (la carpeta o el equipo) para que el render lo muestre.
    """

    filename: str
    content_type: str
    size: int
    sha256: str
    #: Origen legible: la carpeta vigilada, el equipo, "subido a mano"…
    captured_from: str = ""
    #: Fecha del documento si se conoce (mtime del archivo); si no, cuándo se capturó.
    document_date: datetime | None = None
    body_text: str = ""


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


class SocialMediaRef(BaseModel):
    """Referencia a un asset de media de un post social (foto / video).

    La metadata (la `url` del CDN) viaja SIEMPRE en el payload, aunque no se hayan
    bajado los bytes — espejo de cómo `EmailPayload.attachments` lleva metadata sin
    contenido. Cuando `extract_media` está on, los bytes viajan aparte en
    `SourceRecord.media` (`MediaBlob`) y el borde de ingest los sube a MinIO; esta
    ref queda como puntero al origen. `content_type` puede ser `None` cuando el actor
    de Apify no lo informa.

    OJO: las URLs de CDN de redes sociales EXPIRAN, así que sirven para bajar en la
    ingesta (fresco), no como link permanente — re-bajar media vieja puede fallar.
    """

    url: str
    kind: Literal["image", "video"]
    content_type: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class SocialPostPayload(BasePayload):
    """Payload schema para un post público de red social (broadcast).

    Compartido por las tres sources sociales (`instagram`, `facebook`, `x`),
    discriminadas por `platform`. El downstream (classifier / relations/summary.py)
    es agnóstico de plataforma: le importan `text`, `url` y
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
    media_refs: list[SocialMediaRef] = Field(default_factory=list)
    engagement: SocialEngagement | None = None
    is_paid_partnership: bool | None = None
    raw_type: str | None = None
