"""Telegram ingestor (server-side), dos modos:

- **polling** (`TelegramSource`, `Source[TelegramCursor]`): cron-style. Para
  cada `AllowedChat` con `streaming=False`, hace `iter_messages(min_id=cursor)`
  y yieldea records nuevos vía el runner común.
- **streaming** (`TelegramStreamingSource`, `StreamingSource[TelegramCursor]`):
  event-driven. Para `AllowedChat` con `streaming=True`, escucha
  `events.NewMessage` en tiempo real dentro del `StreamingRunner` (lifespan de
  FastAPI). Cubre los chats "instant".

Un mismo source row (`type="telegram"`) puede alimentar AMBOS modos: los chats
streaming=False van por polling, los streaming=True por el listener. Cada modo
excluye los chats del otro — sin doble ingestión.

DMs (chats privados con un usuario) NUNCA se persisten — el parser las rechaza
por política de privacidad.

Auth: requiere un session file pre-autorizado vía `memex-telegram auth`
(comando CLI interactivo, run-once por VPS). El daemon en producción NUNCA
hace prompts interactivos — falla rápido si la session está ausente o expirada.
"""

from __future__ import annotations

from memex.ingestors.telegram.config import (
    AllowedChat,
    TelegramConfig,
    TelegramConfigError,
)
from memex.ingestors.telegram.source import TelegramSource, make_source
from memex.ingestors.telegram.streaming import (
    TelegramStreamingSource,
    make_streaming_source,
)

__all__ = [
    "AllowedChat",
    "TelegramConfig",
    "TelegramConfigError",
    "TelegramSource",
    "TelegramStreamingSource",
    "make_source",
    "make_streaming_source",
]
