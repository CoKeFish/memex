"""Telegram ingestor (server-side, polling).

`TelegramSource` cumple el contrato `Source[TelegramCursor]` y se ingesta vía
el runner común (`memex.ingestors.runner.run_ingestor`). DMs (chats privados
con un usuario) NUNCA se persisten — el parser las rechaza por diseño y
política de privacidad del proyecto.

Modo soportado en Fase 2: **polling** cron-style. Para cada `AllowedChat` con
`streaming=False`, hace `client.iter_messages(min_id=cursor)` y yieldea
records nuevos. Chats con `streaming=True` quedan EXCLUIDOS del polling —
los procesa el `TelegramStreamingSource` event-driven de Fase 3 (todavía no
implementado).

Auth: requiere un session file pre-autorizado vía `memex-telegram auth`
(comando CLI interactivo que dispara el SMS de Telegram una sola vez por
VPS). El daemon en producción NUNCA debe prompts interactivos — falla rápido
si la session está ausente o expirada.
"""

from __future__ import annotations

from memex.ingestors.telegram.config import (
    AllowedChat,
    TelegramConfig,
    TelegramConfigError,
)
from memex.ingestors.telegram.source import TelegramSource, make_source

__all__ = [
    "AllowedChat",
    "TelegramConfig",
    "TelegramConfigError",
    "TelegramSource",
    "make_source",
]
