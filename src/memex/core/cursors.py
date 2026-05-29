"""Typed cursor (checkpoint) models per source type.

Each source defines its cursor shape as a Pydantic model and validates the
dict it reads/writes from memex against it. Catches malformed cursors at the
boundary instead of letting them propagate as KeyErrors mid-fetch.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FolderState(BaseModel):
    """Per-IMAP-folder checkpoint state."""

    uidvalidity: int
    last_uid: int

    model_config = ConfigDict(frozen=True, extra="forbid")


class ImapCursor(BaseModel):
    """IMAP checkpoint: per-folder UIDVALIDITY and last-seen UID.

    When UIDVALIDITY changes for a folder, the source resets `last_uid` to 0
    and emits a warning log so the operator notices the re-fetch.
    """

    folders: dict[str, FolderState] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ChatCursor(BaseModel):
    """Per-chat checkpoint state for Telegram.

    `last_message_id` es el id del último mensaje exitosamente flusheado.
    `iter_messages(min_id=last_message_id)` de Telethon es EXCLUSIVO — yieldea
    solo mensajes con `id > last_message_id`. Si nunca se vio un mensaje,
    arranca en 0 (yieldea todo desde el principio del rango temporal).
    """

    last_message_id: int = 0

    model_config = ConfigDict(frozen=True, extra="forbid")


class TelegramCursor(BaseModel):
    """Telegram checkpoint: por chat permitido, el último message_id visto.

    Las keys del dict son `str(chat_id)` (formato marked de Telethon —
    `str(get_peer_id(chat))` — ej. `"-1001234567890"` para canales) porque
    JSONB convierte ints en strings al deserializar dicts con int keys.
    La conversión a int se hace en el callsite.
    """

    chats: dict[str, ChatCursor] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class AccountCursor(BaseModel):
    """Checkpoint por cuenta social: el post más nuevo ya flusheado.

    `last_post_id` es el id de plataforma del último post posteado para esta
    cuenta; `last_posted_at` su timestamp. El filtro de novedad usa
    `last_posted_at` porque los scrapers no tienen cursor nativo — devuelven los
    últimos N posts y memex filtra client-side lo más nuevo que el cursor.
    `last_post_id` desempata posts del mismo segundo.
    """

    last_post_id: str = ""
    last_posted_at: datetime | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class SocialCursor(BaseModel):
    """Social checkpoint: por cuenta de la allowlist, el último post visto.

    Las keys del dict son el identificador normalizado de la cuenta (handle /
    página en minúsculas, sin `@` ni URL) — el mismo string que la allowlist y
    el `external_id` (`{platform}:{account}:{post_id}`). Compartido por las tres
    sources sociales (`instagram` / `facebook` / `x`); las keys nunca colisionan
    porque cada source solo ve sus propias cuentas.
    """

    accounts: dict[str, AccountCursor] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
