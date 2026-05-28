"""TelegramSource — implementa `Source[TelegramCursor]` (polling).

Una `TelegramSource` = una cuenta de Telegram (una `TelegramConfig`), que
ingesta los chats listados en `cfg.allowed_chats` con `streaming=False`. Los
que tienen `streaming=True` quedan FUERA del polling — los maneja la Fase 3
streaming source con su propio `listen()` event-driven.

Cumple todas las garantías del contrato `Source[CursorT]`:

- `type = "telegram"`, `kind = SourceKind.CHAT`.
- `payload_schema = TelegramPayload`, `config_schema = TelegramConfig`,
  `checkpoint_schema = TelegramCursor` (todos enforce-ados por mypy strict).
- `fetch(checkpoint: TelegramCursor) -> Iterable[SourceRecord]` sincrónico —
  bridgeamos a Telethon (async) vía `client.run_sync(coro)`. Por cada chat
  permitido, colectamos el batch async y lo yieldeamos sync. NO usamos un
  loop compartido entre `fetch()` calls (se crea uno nuevo cada vez).
- `advance_checkpoint(checkpoint, last) -> TelegramCursor` lee
  `chat_id:message_id` desde el `external_id` (sin tocar payload — más
  resiliente a cambios de schema).
- `async health_check() -> HealthResult` abre la session y llama `get_me()`
  — never raises, errors se convierten a `status="unhealthy"`.

Política DM: el parser rechaza chats `User` antes de yieldear. Aunque el
operador meta accidentalmente un user_id en `allowed_chats`, los mensajes
NO se persisten.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from memex.core.cursors import ChatCursor, TelegramCursor
from memex.core.payloads import BasePayload, TelegramPayload
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord
from memex.ingestors.telegram.client import (
    TelegramAuthError,
    TelegramClientWrapper,
    run_sync,
)
from memex.ingestors.telegram.config import TelegramConfig
from memex.ingestors.telegram.parser import parse_telegram_message
from memex.logging import get_logger


class TelegramSource:
    """Polling Source para Telegram. Excluye chats marcados `streaming=True`."""

    type: ClassVar[str] = "telegram"
    kind: ClassVar[SourceKind] = SourceKind.CHAT
    payload_schema: ClassVar[_type[BasePayload]] = TelegramPayload
    config_schema: ClassVar[_type[BaseModel]] = TelegramConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = TelegramCursor

    def __init__(self, cfg: TelegramConfig) -> None:
        self.cfg = cfg
        self._log = get_logger("memex.ingestors.telegram.source").bind(
            phone=cfg.phone_masked,
            session_name=cfg.session_name,
        )

    async def health_check(self) -> HealthResult:
        """Abre la session y confirma autorización. Nunca lanza."""

        async def _probe() -> tuple[Literal["healthy", "unhealthy"], str]:
            try:
                async with TelegramClientWrapper(self.cfg) as tc:
                    me = await tc.get_me()
                return ("healthy", f"login ok, user_id={getattr(me, 'id', '?')}")
            except TelegramAuthError as e:
                return ("unhealthy", f"auth: {e}")
            except Exception as e:
                return ("unhealthy", f"{type(e).__name__}: {e}")

        status, detail = await _probe()
        return HealthResult(
            status=status,
            detail=detail,
            checked_at=datetime.now(UTC),
        )

    def fetch(self, checkpoint: TelegramCursor) -> Iterable[SourceRecord]:
        """Yieldea records nuevos por chat permitido (excluyendo streaming).

        Estrategia sync-over-async: abrimos UNA conexión Telethon por
        invocación de `fetch()`. Por cada chat permitido, drenamos el batch
        completo (colectamos en una lista dentro del context async), después
        yieldeamos sync al runner. No hay event loop reusado entre fetches.
        """
        polling_chats = [c for c in self.cfg.allowed_chats if not c.streaming]
        if not polling_chats:
            self._log.info("telegram.fetch.skip", reason="no_polling_chats")
            return

        self._log.info(
            "telegram.fetch.start",
            chats_count=len(polling_chats),
        )

        async def _collect_all() -> list[tuple[int, list[SourceRecord]]]:
            """Devuelve [(chat_id, records_de_ese_chat), ...]."""
            collected: list[tuple[int, list[SourceRecord]]] = []
            async with TelegramClientWrapper(self.cfg) as tc:
                for allowed in polling_chats:
                    chat_records: list[SourceRecord] = []
                    chat_cursor = checkpoint.chats.get(str(allowed.chat_id))
                    min_id = chat_cursor.last_message_id if chat_cursor else 0
                    chat_log = self._log.bind(chat_id=allowed.chat_id)
                    chat_log.info(
                        "telegram.fetch.chat_start",
                        min_id=min_id,
                    )
                    count_total = 0
                    count_parser_rejected = 0
                    count_topic_rejected = 0
                    count_kept = 0
                    try:
                        async for msg in tc.iter_chat_messages(
                            allowed.chat_id,
                            min_id=min_id,
                            batch_size=self.cfg.batch_size,
                        ):
                            count_total += 1
                            record = parse_telegram_message(msg)
                            if record is None:
                                # Parser descartó (DM, sin id, tipo desconocido, etc.)
                                count_parser_rejected += 1
                                continue
                            topic_id = _topic_id_from_payload(record.payload)
                            if not allowed.matches_topic(topic_id):
                                # Topic fuera de la allowlist del chat.
                                count_topic_rejected += 1
                                continue
                            chat_records.append(record)
                            count_kept += 1
                    except Exception as e:
                        chat_log.warning(
                            "telegram.fetch.chat_error",
                            exc_type=type(e).__name__,
                            exc_msg=str(e),
                            kept_before_error=count_kept,
                        )
                    chat_log.info(
                        "telegram.fetch.chat_end",
                        seen=count_total,
                        kept=count_kept,
                        parser_rejected=count_parser_rejected,
                        topic_rejected=count_topic_rejected,
                    )
                    collected.append((allowed.chat_id, chat_records))
            return collected

        all_batches = run_sync(_collect_all())
        total_yielded = 0
        for _chat_id, records in all_batches:
            for record in records:
                yield record
                total_yielded += 1
        self._log.info(
            "telegram.fetch.end",
            chats_count=len(polling_chats),
            total_yielded=total_yielded,
        )

    def advance_checkpoint(
        self,
        checkpoint: TelegramCursor,
        last: SourceRecord,
    ) -> TelegramCursor:
        """Actualiza `last_message_id` del chat del último record posteado.

        external_id shape: `telegram:<chat_id>:<message_id>`. Si el shape no
        matchea (record de otra source mezclado por error, o malformed),
        retorna el cursor sin cambios — defensivo.
        """
        # Formato estricto: `telegram:<chat_id>:<message_id>` (exactamente 3
        # partes). Cualquier extra colon en el id corrompería el parsing — más
        # seguro rechazar que adivinar.
        parts = last.external_id.split(":")
        if len(parts) != 3 or parts[0] != "telegram":
            return checkpoint
        try:
            chat_id = int(parts[1])
            message_id = int(parts[2])
        except ValueError:
            return checkpoint
        if message_id <= 0:
            return checkpoint
        new_chats = dict(checkpoint.chats)
        new_chats[str(chat_id)] = ChatCursor(last_message_id=message_id)
        return TelegramCursor(chats=new_chats)


def _topic_id_from_payload(payload: dict[str, Any]) -> int | None:
    """Extrae topic_id del payload serializado. None si no aplica."""
    raw = payload.get("topic_id")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def make_source(cfg: dict[str, Any]) -> Source[Any]:
    """SourceFactory para Telegram — valida config dict y retorna `TelegramSource`.

    Matchea el Protocol `SourceFactory`; lo que el registry devuelve cuando
    `resolve("telegram")` es invocado.
    """
    tg_cfg = TelegramConfig.from_source_config(cfg)
    return TelegramSource(tg_cfg)
