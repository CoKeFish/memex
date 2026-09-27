"""Canal `telegram`: manda el aviso por el bot (el MISMO del asistente) al chat configurado.

Solo envía (`sendMessage`): no hace polling, así convive con el asistente que sí lo hace (Telegram
solo exige exclusividad en `getUpdates`). `target` = chat_id (para un chat 1:1 es el id del
usuario, el mismo de `MEMEX_ASSISTANT_ALLOWED_USER_IDS`). Texto plano, troceado a 4000 caracteres.
Un `Bot` se inicializa por envío (`async with`): el daemon manda pocos mensajes por ciclo.
"""

from __future__ import annotations

import logging
from typing import Any

from memex.logging import get_logger
from memex.notifications.channels import ChannelError
from memex.notifications.client import Notification

_log = get_logger("memex.notifications.channels.telegram")
_MAX_MESSAGE = 4000

# La URL de cada request de httpx incluye el token del bot: que no salga en INFO (igual que el
# asistente).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, *, bot: Any | None = None) -> None:
        if not token and bot is None:
            raise ChannelError(
                "falta el token del bot de Telegram (MEMEX_NOTIFICATIONS_TELEGRAM_BOT_TOKEN o "
                "MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN)"
            )
        self._token = token
        self._bot = bot  # inyectable en tests

    async def send(self, target: str, text: str, notification: Notification) -> str | None:
        try:
            chat_id = int(target)
        except ValueError as e:
            raise ChannelError(
                f"destino inválido para telegram (chat_id numérico): {target!r}"
            ) from e
        chunks = [text[i : i + _MAX_MESSAGE] for i in range(0, len(text), _MAX_MESSAGE)] or [""]
        try:
            if self._bot is not None:
                return await self._send_all(self._bot, chat_id, chunks)
            from telegram import Bot

            async with Bot(self._token) as bot:
                return await self._send_all(bot, chat_id, chunks)
        except ChannelError:
            raise
        except Exception as e:  # errores de red/API de Telegram → el servicio registra `fallida`
            raise ChannelError(f"telegram: {e}") from e

    @staticmethod
    async def _send_all(bot: Any, chat_id: int, chunks: list[str]) -> str | None:
        ref: str | None = None
        for chunk in chunks:
            msg = await bot.send_message(chat_id=chat_id, text=chunk)
            ref = str(getattr(msg, "message_id", "")) or ref
        return ref

    async def aclose(self) -> None:
        return None
