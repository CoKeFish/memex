"""Helpers compartidos entre el Source polling (`source.py`) y el
StreamingSource event-driven (`streaming.py`).

Ambos modos comparten exactamente la misma lógica de:

- avanzar el `TelegramCursor` desde el `external_id` de un record,
- extraer el `topic_id` de un payload serializado,
- drenar mensajes de un chat (parse + filtro de topic + counters de
  observabilidad) — el "collect loop",
- el health probe (abrir session + `get_me`).

Extraerlo acá evita dos copias divergentes del corazón de la ingestión de
Telegram. `source.py` lo usa en `fetch()` (polling), `streaming.py` en
`catchup()` (backfill al reconectar). El listener live de `streaming.py`
NO usa el collect loop — parsea cada evento individualmente.

No persiste ni toca DB — solo I/O Telethon + parsing (ADR-001: este módulo
vive en `ingestors/` y por tanto no puede importar inbox/checkpoint/db/api).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from memex.core.cursors import ChatCursor, TelegramCursor
from memex.core.media_types import DEFAULT_MAX_ATTACHMENT_BYTES
from memex.core.source import HealthResult, SourceRecord
from memex.ingestors.telegram._media import download_message_media
from memex.ingestors.telegram.client import TelegramAuthError, TelegramClientWrapper
from memex.ingestors.telegram.config import AllowedChat, TelegramConfig
from memex.ingestors.telegram.parser import parse_telegram_message


@dataclass(frozen=True)
class CollectEvents:
    """Nombres de eventos structlog del collect loop, como literales.

    ADR-007: los event names deben ser literales estáticos (greppables), no
    construidos en runtime. Polling y catchup usan los dos sets de abajo.
    """

    chat_start: str
    chat_end: str
    chat_error: str


FETCH_EVENTS = CollectEvents(
    chat_start="telegram.fetch.chat_start",
    chat_end="telegram.fetch.chat_end",
    chat_error="telegram.fetch.chat_error",
)
CATCHUP_EVENTS = CollectEvents(
    chat_start="telegram.catchup.chat_start",
    chat_end="telegram.catchup.chat_end",
    chat_error="telegram.catchup.chat_error",
)


def topic_id_from_payload(payload: dict[str, Any]) -> int | None:
    """Extrae topic_id del payload serializado. None si no aplica."""
    raw = payload.get("topic_id")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def advance_telegram_checkpoint(
    checkpoint: TelegramCursor,
    last: SourceRecord,
) -> TelegramCursor:
    """Actualiza `last_message_id` del chat del último record posteado.

    external_id shape estricto: `telegram:<chat_id>:<message_id>` (exactamente
    3 partes). Si no matchea (record de otra source, o malformed), retorna el
    cursor sin cambios — defensivo.
    """
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


async def collect_chat_records(
    tc: TelegramClientWrapper,
    allowed: AllowedChat,
    *,
    min_id: int,
    batch_size: int,
    log: Any,
    events: CollectEvents,
    extract_media: bool = False,
    max_image_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_video_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> list[SourceRecord]:
    """Drena mensajes con `id > min_id` de un chat, parseando y filtrando topic.

    Compartido por polling (`fetch`) y catchup streaming. Un error a mitad de
    chat se loggea pero no propaga — devuelve lo colectado hasta ahí (el cursor
    avanzará solo hasta el último record exitoso; el resto se re-fetchea en la
    próxima pasada). `events` trae los nombres literales de los structlog
    events según el modo (`FETCH_EVENTS` o `CATCHUP_EVENTS`).

    Si `extract_media`, baja los bytes de la media (foto/video/documento
    imagen·PDF) de cada mensaje y los adjunta al record (best-effort, NUNCA
    tumba el chat). Los topes `max_image_bytes`/`max_video_bytes` solo aplican en
    ese caso; sus defaults son placeholders para el path `extract_media=False`
    (los callers reales pasan los valores de `cfg` junto con `extract_media`).
    """
    chat_log = log.bind(chat_id=allowed.chat_id)
    chat_log.info(events.chat_start, min_id=min_id)

    records: list[SourceRecord] = []
    seen = 0
    parser_rejected = 0
    topic_rejected = 0
    try:
        async for msg in tc.iter_chat_messages(
            allowed.chat_id,
            min_id=min_id,
            batch_size=batch_size,
        ):
            seen += 1
            record = parse_telegram_message(msg)
            if record is None:
                parser_rejected += 1
                continue
            if not allowed.matches_topic(topic_id_from_payload(record.payload)):
                topic_rejected += 1
                continue
            if extract_media:
                blobs = await download_message_media(
                    msg,
                    tc=tc,
                    max_image_bytes=max_image_bytes,
                    max_video_bytes=max_video_bytes,
                    log=chat_log,
                )
                if blobs:
                    record = replace(record, media=blobs)
            records.append(record)
    except Exception as e:
        chat_log.warning(
            events.chat_error,
            exc_type=type(e).__name__,
            exc_msg=str(e),
            kept_before_error=len(records),
        )
    chat_log.info(
        events.chat_end,
        seen=seen,
        kept=len(records),
        parser_rejected=parser_rejected,
        topic_rejected=topic_rejected,
    )
    return records


async def telegram_health_probe(cfg: TelegramConfig) -> HealthResult:
    """Abre la session y confirma autorización vía `get_me`. Nunca lanza.

    Compartido por el health_check de `TelegramSource` y
    `TelegramStreamingSource` — son idénticos (la salud de la cuenta no
    depende del modo de ingestión).
    """

    async def _probe() -> tuple[Literal["healthy", "unhealthy"], str]:
        try:
            async with TelegramClientWrapper(cfg) as tc:
                me = await tc.get_me()
            return ("healthy", f"login ok, user_id={getattr(me, 'id', '?')}")
        except TelegramAuthError as e:
            return ("unhealthy", f"auth: {e}")
        except Exception as e:
            return ("unhealthy", f"{type(e).__name__}: {e}")

    status, detail = await _probe()
    return HealthResult(status=status, detail=detail, checked_at=datetime.now(UTC))
