"""Contexto de CHAT para el procesamiento: la naturaleza de cada chat permitido y quién habla.

La allowlist de la fuente (`sources.config.allowed_chats[].mode`, ver `ingestors/telegram/config`)
declara la naturaleza de cada chat: `anuncios` (comunidad: solo anuncios del organizador y lo que
involucre al usuario), `programa` (el usuario participa un tiempo: estados y anuncios del programa)
o `intimo` (grupo cercano: todo cuenta, todos ganan ficha). `sources.config.chat_mode_default`
aplica a los chats que no figuren en la lista. Los módulos la leen al procesar (no se estampa en
el payload) para que cambiarla no exija re-ingerir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

CHAT_MODES = ("anuncios", "programa", "intimo")
DEFAULT_CHAT_MODE = "anuncios"

#: Con cuántos mensajes en el chat un remitente de `anuncios`/`programa` gana ficha (recurrente).
RECURRENT_SENDER_MIN_MSGS = 5

_LABELS = {
    "anuncios": "chat de comunidad (solo anuncios)",
    "programa": "programa en el que el usuario participa",
    "intimo": "grupo cercano del usuario",
}


def mode_label(mode: str) -> str:
    return _LABELS.get(mode, _LABELS[DEFAULT_CHAT_MODE])


def _valid(mode: Any) -> str:
    m = str(mode or DEFAULT_CHAT_MODE)
    return m if m in CHAT_MODES else DEFAULT_CHAT_MODE


@dataclass(frozen=True)
class ChatModes:
    """Naturaleza por chat de UNA fuente + default para los que no figuran en la allowlist."""

    #: clave = `str(chat_id)`: Telegram usa ids numéricos y WhatsApp JIDs (`123@g.us`).
    by_chat: dict[str, str] = field(default_factory=dict)
    default: str = DEFAULT_CHAT_MODE

    def of(self, payload: dict[str, Any]) -> str:
        chat_id = payload.get("chat_id")
        if chat_id is None:
            return self.default
        return self.by_chat.get(str(chat_id), self.default)

    def of_chat(self, chat_id: int | str | None) -> str:
        return self.by_chat.get(str(chat_id), self.default) if chat_id is not None else self.default


def window_modes(conn: Connection, source_id: int, payloads: list[dict[str, Any]]) -> set[str]:
    """Naturalezas presentes en una ventana de chat (por sus payloads)."""
    modes = chat_modes_for_source(conn, source_id)
    return {modes.of(p) for p in payloads}


def chat_modes_for_source(conn: Connection, source_id: int) -> ChatModes:
    cfg = conn.execute(
        text("SELECT config FROM sources WHERE id = :sid"), {"sid": source_id}
    ).scalar()
    if not isinstance(cfg, dict):
        return ChatModes()
    by_chat: dict[str, str] = {}
    for entry in cfg.get("allowed_chats") or []:
        if not isinstance(entry, dict) or entry.get("chat_id") in (None, ""):
            continue
        by_chat[str(entry.get("chat_id"))] = _valid(entry.get("mode"))
    return ChatModes(by_chat=by_chat, default=_valid(cfg.get("chat_mode_default")))
