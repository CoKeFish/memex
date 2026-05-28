"""Bootstrap del streaming — composition root del lifespan de FastAPI.

Vive en la capa API (NO en ingestors) porque arma la cadena que cruza la
frontera ADR-001: lee la tabla `sources`, compone el handler chain con
persistencia (`PersistMiddleware` toca inbox) y el filtro pre-ingest, y
construye el `StreamingRunner`. Los ingestors no pueden importar nada de esto;
acá sí — es el lado "interno".

Qué hace `build_streaming_runner()`:

1. Lee los sources `type='telegram'` habilitados de la DB.
2. Por cada uno con ≥1 chat `streaming=True`, construye un
   `TelegramStreamingSource` + un handler chain
   `[DeterministicFilterMiddleware, PersistMiddleware]`.
3. Devuelve un `StreamingRunner` listo para `start()`/`stop()`.

Resiliencia: si la recolección de sources falla (DB caída al boot, config
inválida), loggea y devuelve un runner vacío en vez de tumbar el arranque del
API. El streaming arranca en el próximo restart; el API sigue sirviendo HTTP.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.core import checkpoint, filters
from memex.core.filters import DeterministicFilterMiddleware, FilterRule
from memex.core.ingest_middlewares import PersistMiddleware, noop_terminal
from memex.core.middleware import IngestContext, build_handler
from memex.core.source import SourceConfigError
from memex.core.streaming_runner import RegisteredStreamingSource, StreamingRunner
from memex.db import connection
from memex.ingestors.telegram.config import TelegramConfig
from memex.ingestors.telegram.streaming import TelegramStreamingSource
from memex.logging import get_logger

_log = get_logger("memex.api.streaming")


def _load_rules(user_id: int, source_type: str | None, source_id: int | None) -> list[FilterRule]:
    with connection() as conn:
        return filters.load_active_rules(
            conn,
            user_id=user_id,
            source_type=source_type,
            source_id=source_id,
        )


def _load_checkpoint(source_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        return checkpoint.get_cursor(conn, source_id)


def _save_checkpoint(source_id: int, cursor: dict[str, Any]) -> None:
    with connection() as conn:
        checkpoint.save_cursor(conn, source_id, cursor)


def _collect_telegram_streaming_sources() -> list[RegisteredStreamingSource]:
    """Lee la DB y arma un RegisteredStreamingSource por source telegram con
    al menos un chat streaming. Config inválida → log + skip (no crash)."""
    with connection() as conn:
        rows = (
            conn.execute(
                text("SELECT id, user_id, config FROM sources WHERE type = 'telegram' AND enabled")
            )
            .mappings()
            .all()
        )

    registered: list[RegisteredStreamingSource] = []
    for row in rows:
        source_id = int(row["id"])
        user_id = int(row["user_id"])
        cfg_dict = row["config"] or {}
        try:
            tg_cfg = TelegramConfig.from_source_config(cfg_dict)
        except SourceConfigError as e:
            _log.error(
                "streaming.bootstrap.config_invalid",
                source_id=source_id,
                reason=str(e),
            )
            continue

        streaming_count = sum(1 for c in tg_cfg.allowed_chats if c.streaming)
        if streaming_count == 0:
            continue  # polling-only source — no streaming listener needed

        source = TelegramStreamingSource(tg_cfg)
        ctx = IngestContext(source_id=source_id, source_type="telegram", user_id=user_id)
        handler = build_handler(
            [DeterministicFilterMiddleware(_load_rules), PersistMiddleware()],
            noop_terminal,
            ctx,
        )
        registered.append(
            RegisteredStreamingSource(source=source, source_id=source_id, handler=handler)
        )
        _log.info(
            "streaming.bootstrap.registered",
            source_id=source_id,
            streaming_chats=streaming_count,
        )
    return registered


def build_streaming_runner() -> StreamingRunner:
    """Construye el StreamingRunner para el lifespan. Nunca lanza al boot."""
    try:
        registered = _collect_telegram_streaming_sources()
    except Exception as e:
        _log.error(
            "streaming.bootstrap.failed",
            exc_type=type(e).__name__,
            exc_msg=str(e),
        )
        registered = []
    return StreamingRunner(
        registered,
        load_checkpoint=_load_checkpoint,
        save_checkpoint=_save_checkpoint,
    )
