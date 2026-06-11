"""TelegramSource — implementa `Source[TelegramCursor]` (polling).

Una `TelegramSource` = una cuenta de Telegram (una `TelegramConfig`), que
ingesta los chats listados en `cfg.allowed_chats` con `streaming=False`. Los
que tienen `streaming=True` quedan FUERA del polling — los maneja la Fase 3
streaming source (`TelegramStreamingSource`) con su propio `listen()`.

Cumple todas las garantías del contrato `Source[CursorT]`:

- `type = "telegram"`, `kind = SourceKind.CHAT`.
- `payload_schema = TelegramPayload`, `config_schema = TelegramConfig`,
  `checkpoint_schema = TelegramCursor` (todos enforce-ados por mypy strict).
- `fetch(checkpoint: TelegramCursor) -> Iterable[SourceRecord]` sincrónico —
  bridgeamos a Telethon (async) vía `run_sync(coro)`. Por cada chat
  permitido, colectamos el batch async y lo yieldeamos sync. No reusamos un
  event loop entre `fetch()` calls (se crea uno nuevo cada vez).
- `advance_checkpoint(checkpoint, last) -> TelegramCursor` lee
  `chat_id:message_id` desde el `external_id` (compartido vía `_common`).
- `async health_check() -> HealthResult` abre la session y llama `get_me()`
  (compartido vía `_common`) — never raises.

La lógica de collect/advance/health vive en `_common.py` y la comparten
polling y streaming para evitar dos copias divergentes.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from pydantic import BaseModel

from memex.core.cursors import TelegramCursor
from memex.core.payloads import BasePayload, TelegramPayload
from memex.core.source import HealthResult, Source, SourceKind, SourceRecord
from memex.ingestors.telegram._common import (
    FETCH_EVENTS,
    advance_telegram_checkpoint,
    collect_chat_records,
    telegram_health_probe,
)
from memex.ingestors.telegram.client import TelegramClientWrapper, run_sync
from memex.ingestors.telegram.config import TelegramConfig
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
        self._log = get_logger(
            "memex.ingestors.telegram.source",
            phone=cfg.phone_masked,
            session_name=cfg.session_name,
        )

    async def health_check(self) -> HealthResult:
        """Abre la session y confirma autorización. Nunca lanza."""
        return await telegram_health_probe(self.cfg)

    def fetch(self, checkpoint: TelegramCursor) -> Iterable[SourceRecord]:
        """Yieldea records nuevos por chat permitido (excluyendo streaming).

        Estrategia sync-over-async: abrimos UNA conexión Telethon por
        invocación de `fetch()`. Por cada chat permitido, drenamos el batch
        completo (colectamos en una lista dentro del context async), después
        yieldeamos sync al runner.
        """
        polling_chats = [c for c in self.cfg.allowed_chats if not c.streaming]
        if not polling_chats:
            self._log.info("telegram.fetch.skip", reason="no_polling_chats")
            return

        self._log.info("telegram.fetch.start", chats_count=len(polling_chats))

        async def _collect_all() -> list[SourceRecord]:
            collected: list[SourceRecord] = []
            async with TelegramClientWrapper(self.cfg) as tc:
                for allowed in polling_chats:
                    chat_cursor = checkpoint.chats.get(str(allowed.chat_id))
                    min_id = chat_cursor.last_message_id if chat_cursor else 0
                    records = await collect_chat_records(
                        tc,
                        allowed,
                        min_id=min_id,
                        batch_size=self.cfg.batch_size,
                        log=self._log,
                        events=FETCH_EVENTS,
                        extract_media=self.cfg.extract_media,
                        max_image_bytes=self.cfg.max_attachment_bytes,
                        max_video_bytes=self.cfg.max_video_bytes,
                    )
                    collected.extend(records)
            return collected

        all_records = run_sync(_collect_all())
        yield from all_records
        self._log.info(
            "telegram.fetch.end",
            chats_count=len(polling_chats),
            total_yielded=len(all_records),
        )

    def advance_checkpoint(
        self,
        checkpoint: TelegramCursor,
        last: SourceRecord,
    ) -> TelegramCursor:
        """Actualiza `last_message_id` del chat del último record posteado."""
        return advance_telegram_checkpoint(checkpoint, last)


def make_source(cfg: dict[str, Any], env: Mapping[str, str] | None = None) -> Source[Any]:
    """SourceFactory para Telegram — valida config dict y retorna `TelegramSource`.

    Matchea el Protocol `SourceFactory`; lo que el registry devuelve cuando
    `resolve("telegram")` es invocado. `env` (secretos resueltos del vault, o None
    para el fallback os.environ) se pasa a `TelegramConfig.from_source_config`.
    """
    tg_cfg = TelegramConfig.from_source_config(cfg, env)
    return TelegramSource(tg_cfg)
