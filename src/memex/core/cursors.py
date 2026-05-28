"""Typed cursor (checkpoint) models per source type.

Each source defines its cursor shape as a Pydantic model and validates the
dict it reads/writes from memex against it. Catches malformed cursors at the
boundary instead of letting them propagate as KeyErrors mid-fetch.
"""

from __future__ import annotations

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
