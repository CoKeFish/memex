"""Middlewares concretos que tocan persistencia — viven en core, no en ingestors.

Por qué acá y no en `memex.ingestors.*`: la disciplina de ADR-001
(`tests/test_typing_discipline.py`) prohíbe que los ingestors importen
`memex.core.inbox`, `memex.core.checkpoint`, `memex.db` o `memex.api`. Un
middleware que persiste SÍ necesita `inbox.insert_record`, así que vive en
`memex.core`, que es el lado "interno" de esa frontera.

`PersistMiddleware` es el terminal del handler chain que el `StreamingRunner`
arma para fuentes event-driven (Telegram streaming, IMAP IDLE futuro). El
chain típico es:

    build_handler(
        [DeterministicFilterMiddleware(load_rules), PersistMiddleware()],
        _noop_terminal,
        IngestContext(source_id=..., source_type=..., user_id=...),
    )

donde `DeterministicFilterMiddleware` (de `memex.core.filters`) dropea o pasa,
y `PersistMiddleware` escribe en `inbox`. PersistMiddleware NO llama `next`
(es terminal) y NO avanza el checkpoint — eso lo hace el runner después de
que el handler retorna OK (`_RunningSource.on_record`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from memex.core.inbox import InsertResult, insert_record
from memex.core.middleware import IngestContext, Next
from memex.core.source import SourceRecord
from memex.db import connection
from memex.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy import Connection

ConnectionFactory = Callable[[], AbstractContextManager["Connection"]]
"""Devuelve un context manager de conexión (autocommit on success). Default:
`memex.db.connection`. Inyectable para tests."""

_log = get_logger("memex.core.ingest_middlewares")


class PersistMiddleware:
    """Terminal middleware: persiste el record en `inbox`. No llama `next`.

    Implementa el Protocol `memex.core.middleware.IngestMiddleware`
    estructuralmente (no por herencia — el Protocol es `runtime_checkable`).

    El insert es síncrono (SQLAlchemy Connection), así que lo corremos en un
    threadpool con `asyncio.to_thread` para no bloquear el event loop del
    listener mientras espera el round-trip a Postgres.

    Idempotente: `inbox.insert_record` hace ON CONFLICT DO NOTHING sobre
    `(source_id, external_id)`. Un duplicado loggea `persist.dedupe_conflict`
    y NO es error — el runner igual avanza el cursor (ya vimos ese mensaje).
    """

    def __init__(self, connection_factory: ConnectionFactory = connection) -> None:
        self._connection = connection_factory

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None:
        result = await asyncio.to_thread(self._persist, record, ctx)
        if result.inserted:
            _log.info(
                "persist.inserted",
                source_id=ctx.source_id,
                source_type=ctx.source_type,
                inbox_id=result.id,
            )
        else:
            # external_id es `telegram:<chat_id>:<message_id>` — sin contenido.
            _log.info(
                "persist.dedupe_conflict",
                source_id=ctx.source_id,
                source_type=ctx.source_type,
                external_id=record.external_id,
            )
        # Terminal: NO llamamos `next` — somos el final de la chain.

    def _persist(self, record: SourceRecord, ctx: IngestContext) -> InsertResult:
        with self._connection() as conn:
            return insert_record(
                conn,
                user_id=ctx.user_id,
                source_id=ctx.source_id,
                record=record,
            )


async def noop_terminal(record: SourceRecord) -> None:
    """Terminal vacío para `build_handler` cuando el último middleware ya
    persiste (PersistMiddleware no llama `next`, así que este terminal nunca
    se alcanza — existe solo para satisfacer la firma de `build_handler`)."""
    _ = record
