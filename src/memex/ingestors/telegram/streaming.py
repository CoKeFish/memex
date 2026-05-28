"""TelegramStreamingSource — implementa `StreamingSource[TelegramCursor]`.

Event-driven, para chats marcados `streaming=True` en la config. Vive dentro
de un task del `StreamingRunner` (arrancado por el lifespan de FastAPI).
Cubre los chats "instant" — los pocos donde la latencia de polling (minutos)
no sirve y el usuario quiere reaccionar al toque.

Ciclo de vida (orquestado por el runner):

  1. `catchup(cursor)` — drena lo que pasó offline vía `iter_messages(min_id)`
     por cada chat streaming. Single-threaded, sin race con eventos live
     (el handler todavía no está registrado). El runner avanza el cursor por
     record.
  2. `listen(on_record)` — abre un cliente con `sequential_updates=True`,
     registra un handler `events.NewMessage(chats=streaming_ids)`, y bloquea en
     `run_until_disconnected()`. Cada evento: resuelve chat/sender, parsea,
     filtra topic, y llama `on_record` (la chain filter+persist del runner).
  3. `disconnect()` — desde el `stop()` del runner (otra task): corta el
     `run_until_disconnected` limpio.

Gap conocido (documentado): entre el fin de `catchup` y el registro del
handler hay una ventana (~1s, una vez por vida de conexión) donde un mensaje
podría no disparar el handler. Se recupera en el próximo `catchup`
(reconnect/restart). El dedup de `inbox` (ON CONFLICT) hace inocuo el
solapamiento catchup/live. Endurecimiento futuro: register-first-then-backfill
dentro de `listen` (requiere serializar backfill vs live con un lock).

Política de errores en el handler live: si `on_record` (persist) lanza, el
handler loggea, guarda la excepción y desconecta — `listen()` la re-lanza al
salir para que el supervisor reconecte. En el reconnect, `catchup` re-pulls
desde el último cursor bueno (el fallido no avanzó el cursor) y el dedup
evita duplicados. Errores persistentes dead-letter tras `max_retries`.

ADR-001: este módulo NO importa inbox/checkpoint/db/api. Solo produce records
y los pasa al `on_record` que el runner inyecta.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel

from memex.core.cursors import TelegramCursor
from memex.core.payloads import BasePayload, TelegramPayload
from memex.core.source import HealthResult, SourceKind, SourceRecord
from memex.core.streaming import StreamHandler, StreamingSource
from memex.ingestors.telegram._common import (
    CATCHUP_EVENTS,
    advance_telegram_checkpoint,
    collect_chat_records,
    telegram_health_probe,
    topic_id_from_payload,
)
from memex.ingestors.telegram.client import TelegramClientWrapper
from memex.ingestors.telegram.config import AllowedChat, TelegramConfig
from memex.ingestors.telegram.parser import parse_telegram_message
from memex.logging import get_logger


class TelegramStreamingSource:
    """StreamingSource event-driven para chats Telegram marcados `streaming=True`."""

    type: ClassVar[str] = "telegram"
    kind: ClassVar[SourceKind] = SourceKind.CHAT
    payload_schema: ClassVar[_type[BasePayload]] = TelegramPayload
    config_schema: ClassVar[_type[BaseModel]] = TelegramConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = TelegramCursor

    def __init__(self, cfg: TelegramConfig) -> None:
        self.cfg = cfg
        self._log = get_logger("memex.ingestors.telegram.streaming").bind(
            phone=cfg.phone_masked,
            session_name=cfg.session_name,
        )
        self._wrapper: TelegramClientWrapper | None = None

    def _streaming_chats(self) -> list[AllowedChat]:
        return [c for c in self.cfg.allowed_chats if c.streaming]

    async def health_check(self) -> HealthResult:
        """Abre la session y confirma autorización. Nunca lanza."""
        return await telegram_health_probe(self.cfg)

    def advance_checkpoint(
        self,
        checkpoint: TelegramCursor,
        last: SourceRecord,
    ) -> TelegramCursor:
        """Actualiza `last_message_id` del chat del último record procesado."""
        return advance_telegram_checkpoint(checkpoint, last)

    async def catchup(self, checkpoint: TelegramCursor) -> AsyncIterator[SourceRecord]:
        """Drena lo que pasó offline en los chats streaming antes de escuchar.

        Abre su propio cliente (separado del de `listen`). Single-threaded:
        no hay handler registrado, así que no compite con eventos live.

        Colectamos TODOS los records dentro del `async with` y recién después
        yieldeamos — así la conexión Telethon se abre y cierra de forma
        determinística, sin quedar viva durante los yields. Esto evita una
        fuga de conexión si el consumidor (el runner) abandona el generador a
        mitad (ej. cancelación por `stop()`).
        """
        streaming_chats = self._streaming_chats()
        if not streaming_chats:
            self._log.info("streaming.catchup.skip", reason="no_streaming_chats")
            return

        collected: list[SourceRecord] = []
        async with TelegramClientWrapper(self.cfg) as tc:
            for allowed in streaming_chats:
                chat_cursor = checkpoint.chats.get(str(allowed.chat_id))
                min_id = chat_cursor.last_message_id if chat_cursor else 0
                collected.extend(
                    await collect_chat_records(
                        tc,
                        allowed,
                        min_id=min_id,
                        batch_size=self.cfg.batch_size,
                        log=self._log,
                        events=CATCHUP_EVENTS,
                    )
                )
        # Conexión ya cerrada — yieldear no sostiene recursos.
        self._log.info("streaming.catchup", count=len(collected))
        for record in collected:
            yield record

    async def listen(self, on_record: StreamHandler) -> None:
        """Escucha eventos live indefinidamente. Bloquea hasta `disconnect()`.

        Si `on_record` lanza en un evento, lo guardamos y desconectamos para
        que el supervisor reconecte (y `catchup` recupere desde el cursor).
        """
        streaming_chats = self._streaming_chats()
        if not streaming_chats:
            self._log.info("streaming.listen.skip", reason="no_streaming_chats")
            return

        chat_ids = [c.chat_id for c in streaming_chats]
        by_id = {c.chat_id: c for c in streaming_chats}
        handler_error: list[BaseException] = []

        async with TelegramClientWrapper(self.cfg, sequential_updates=True) as tc:
            self._wrapper = tc

            async def _on_event(event: Any) -> None:
                try:
                    chat = await event.get_chat()
                    sender = await event.get_sender()
                    record = parse_telegram_message(event.message, chat=chat, sender=sender)
                    if record is None:
                        return
                    chat_id = _chat_id_of(record)
                    allowed = by_id.get(chat_id) if chat_id is not None else None
                    if allowed is None:
                        # Evento de un chat que no streameamos (no debería pasar
                        # dado el filtro chats=, pero defensivo).
                        return
                    if not allowed.matches_topic(topic_id_from_payload(record.payload)):
                        return
                    self._log.info("streaming.event_received", chat_id=chat_id)
                    await on_record(record)
                except Exception as e:
                    self._log.warning(
                        "streaming.event_error",
                        exc_type=type(e).__name__,
                        exc_msg=str(e),
                    )
                    handler_error.append(e)
                    await tc.disconnect()

            tc.add_new_message_handler(_on_event, chat_ids)
            self._log.info("streaming.connected", chats_count=len(chat_ids))
            await tc.run_until_disconnected()

        self._wrapper = None
        self._log.info("streaming.disconnected")
        if handler_error:
            # Re-lanzar para que el supervisor del runner reconecte con backoff.
            raise handler_error[0]

    async def disconnect(self) -> None:
        """Corta el listener limpiamente. Idempotente, seguro desde otra task."""
        if self._wrapper is not None:
            await self._wrapper.disconnect()


def _chat_id_of(record: SourceRecord) -> int | None:
    """Lee el chat_id normalizado del payload del record."""
    raw = record.payload.get("chat_id")
    return raw if isinstance(raw, int) else None


def make_streaming_source(cfg: dict[str, Any]) -> StreamingSource[Any]:
    """Factory: valida config dict y retorna `TelegramStreamingSource`.

    Usada por el bootstrap del lifespan (api layer) y por el CLI `listen`.
    """
    tg_cfg = TelegramConfig.from_source_config(cfg)
    return TelegramStreamingSource(tg_cfg)
