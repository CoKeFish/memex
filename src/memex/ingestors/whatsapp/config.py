"""Configuración de WhatsApp: dónde vive la sesión y qué chats entran.

La sesión (claves del dispositivo vinculado) es un sqlite de whatsmeow por cuenta:
`<MEMEX_WA_SESSION_PATH>/account_<id>.db`. Es tan sensible como la sesión de Telethon: da acceso a
todos los chats. En compose apunta al mount de `./secrets` (fuera de git).

La allowlist vive en `sources.config.allowed_chats` con el MISMO formato que Telegram —`chat_id`
(acá el JID, texto), `mode` (naturaleza: anuncios | programa | intimo)— para que
`modules.chat_context` la lea sin cambios; suma `since` (solo mensajes desde esa fecha) y `name`
(rótulo para la UI, no autoritativo).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from memex.modules.chat_context import CHAT_MODES, DEFAULT_CHAT_MODE

SESSION_PATH_ENV = "MEMEX_WA_SESSION_PATH"
_DEFAULT_SESSION_DIR = "secrets/whatsapp"

#: Tope de adjunto por mensaje (imágenes/documentos); los videos más grandes no se bajan.
DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
#: Retención de la bandeja de chats no incluidos.
DEFAULT_BUFFER_RETENTION_DAYS = 30

ChatMode = Literal["anuncios", "programa", "intimo"]


def session_dir() -> Path:
    return Path(os.environ.get(SESSION_PATH_ENV, _DEFAULT_SESSION_DIR))


def session_db_path(account_id: int) -> Path:
    return session_dir() / f"account_{int(account_id)}.db"


class WaAllowedChat(BaseModel):
    """Un chat incluido: su JID, la naturaleza y desde qué fecha entran sus mensajes."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    chat_id: str = Field(min_length=3)
    name: str = ""
    mode: ChatMode = "anuncios"
    since: date | None = None

    def admits(self, when: date) -> bool:
        return self.since is None or when >= self.since


def parse_allowed_chats(config: dict[str, Any] | None) -> dict[str, WaAllowedChat]:
    """`sources.config.allowed_chats` → {jid: WaAllowedChat}. Entradas inválidas se ignoran; un
    `mode` desconocido cae al default (igual que `chat_context._valid`)."""
    out: dict[str, WaAllowedChat] = {}
    for entry in (config or {}).get("allowed_chats") or []:
        if not isinstance(entry, dict) or not entry.get("chat_id"):
            continue
        data = dict(entry)
        data["chat_id"] = str(data["chat_id"])
        if data.get("mode") not in CHAT_MODES:
            data["mode"] = DEFAULT_CHAT_MODE
        if data.get("since") in ("", None):
            data["since"] = None
        try:
            chat = WaAllowedChat.model_validate(data)
        except ValueError:
            continue
        out[chat.chat_id] = chat
    return out


def allowed_chats_to_config(chats: list[WaAllowedChat]) -> list[dict[str, Any]]:
    return [
        {
            "chat_id": c.chat_id,
            "name": c.name,
            "mode": c.mode,
            "since": c.since.isoformat() if c.since else None,
        }
        for c in chats
    ]
