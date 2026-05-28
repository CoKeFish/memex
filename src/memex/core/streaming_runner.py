"""StreamingRunner — supervisor de listeners persistentes.

Gestiona el ciclo de vida de uno o más `StreamingSource` registrados:

  1. Carga checkpoint inicial desde el store inyectado.
  2. Invoca `catchup(cursor)` para drenar lo que pasó offline. Cada record
     pasa por el handler chain y avanza el cursor.
  3. Invoca `listen(handler)`, que bloquea hasta `disconnect()` o excepción.
  4. Si `listen` o `catchup` lanzan, reconnecta con backoff exponencial
     (1s → 2s → 4s → ... → cap configurable). Después de N intentos
     consecutivos sin éxito, declara el source como dead-letter.

Pensado para vivir dentro del lifespan async de FastAPI. La `app.py` arma la
chain de middlewares, registra los sources streaming, y llama `start()` al
startup / `stop()` al shutdown.

Diseño:

  * El runner NO conoce inbox, filter_rules ni ningún detalle de
    persistencia — el handler chain (construido con
    `memex.core.middleware.build_handler`) encapsula todo eso. El runner
    solo se ocupa de: catchup → listen → reconnect → cleanup, y de mantener
    el cursor avanzado entre records.
  * El checkpoint store es inyectado vía dos callables (load/save), no un
    Protocol grande. Esto deja el runner testeable con un dict in-memory
    sin DB.
  * No usa threads ni multiprocess. Es 100% asyncio.

Observabilidad emitida:

  * `streaming_runner.started` (count de sources registrados).
  * `streaming_runner.stopped`.
  * `streaming_runner.source_started` (source_id).
  * `streaming_runner.source_catchup_done` (source_id, count).
  * `streaming_runner.source_reconnect` (source_id, retry, backoff_s, error).
  * `streaming_runner.source_dead_letter` (source_id, retries) — el source
    queda inerte hasta el próximo restart del runner.
  * `streaming_runner.source_stopped` (source_id, reason: "disconnect" |
    "dead_letter" | "cancelled").
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from memex.core.source import SourceRecord
from memex.core.streaming import StreamHandler, StreamingSource
from memex.logging import get_logger

CheckpointLoad = Callable[[int], dict[str, Any] | None]
"""Lee el cursor crudo (JSONB → dict) del store para un source_id."""

CheckpointSave = Callable[[int, dict[str, Any]], None]
"""Persiste el cursor crudo (dict → JSONB) para un source_id."""


@dataclass
class RegisteredStreamingSource:
    """Atan un `StreamingSource` con su source_id y handler en runtime.

    `handler` es la chain compuesta (filter + persist + ...) que el runner
    invoca por cada record. NO incluye el avance del cursor — eso lo hace
    el runner internamente después de que el handler retorna OK.
    """

    source: StreamingSource[Any]
    source_id: int
    handler: StreamHandler


@dataclass
class _RunningSource:
    """Estado per-source que el supervisor mantiene a lo largo del ciclo de vida.

    Encapsula el cursor mutable (avanza entre records) para evitar el patrón
    de closure-con-nonlocal que es propenso a bugs si el handler async se
    invoca concurrentemente.
    """

    registered: RegisteredStreamingSource
    cursor: BaseModel
    save: CheckpointSave
    log: Any = field(repr=False)
    catchup_count: int = 0

    async def on_record(self, record: SourceRecord) -> None:
        await self.registered.handler(record)
        self.cursor = self.registered.source.advance_checkpoint(self.cursor, record)
        self.save(self.registered.source_id, self.cursor.model_dump(mode="json"))

    async def run_once(self) -> None:
        """Catchup → listen. Lanza si algo falla; el supervisor decide reconnect."""
        self.catchup_count = 0
        async for record in self.registered.source.catchup(self.cursor):
            await self.on_record(record)
            self.catchup_count += 1
        self.log.info(
            "streaming_runner.source_catchup_done",
            source_id=self.registered.source_id,
            count=self.catchup_count,
        )
        await self.registered.source.listen(self.on_record)


class StreamingRunner:
    """Supervisor de uno o más `StreamingSource`s.

    Uso típico desde el lifespan de FastAPI:

        runner = StreamingRunner(
            [RegisteredStreamingSource(src, source_id=1, handler=chain), ...],
            load_checkpoint=lambda sid: checkpoint.get_cursor(conn, sid),
            save_checkpoint=lambda sid, c: checkpoint.save_cursor(conn, sid, c),
        )
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()
    """

    def __init__(
        self,
        sources: list[RegisteredStreamingSource],
        load_checkpoint: CheckpointLoad,
        save_checkpoint: CheckpointSave,
        *,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        max_retries: int = 10,
        stop_timeout_s: float = 30.0,
    ) -> None:
        self._sources = sources
        self._load = load_checkpoint
        self._save = save_checkpoint
        self._initial_backoff = initial_backoff_s
        self._max_backoff = max_backoff_s
        self._max_retries = max_retries
        self._stop_timeout = stop_timeout_s
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._log = get_logger("memex.core.streaming_runner")

    async def start(self) -> None:
        """Arranca un task asyncio por cada source registrado.

        No-op si la lista está vacía (no costo si nadie configuró streaming).
        """
        if not self._sources:
            self._log.info("streaming_runner.started", count=0)
            return
        for reg in self._sources:
            task = asyncio.create_task(
                self._supervise(reg),
                name=f"streaming-{reg.source.type}-{reg.source_id}",
            )
            self._tasks.append(task)
        self._log.info("streaming_runner.started", count=len(self._tasks))

    async def stop(self) -> None:
        """Cancela tasks, espera con timeout, llama disconnect() en cada source."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=self._stop_timeout,
                )
            except TimeoutError:
                self._log.warning(
                    "streaming_runner.stop_timeout",
                    timeout_s=self._stop_timeout,
                )
        for reg in self._sources:
            try:
                await reg.source.disconnect()
            except Exception as e:
                self._log.warning(
                    "streaming_runner.disconnect_failed",
                    source_id=reg.source_id,
                    error=str(e),
                )
        self._log.info("streaming_runner.stopped", count=len(self._tasks))
        self._tasks = []

    async def _supervise(self, reg: RegisteredStreamingSource) -> None:
        """Run-loop con backoff. Reconnecta hasta dead-letter o stop."""
        cursor_raw = self._load(reg.source_id) or {}
        cursor: BaseModel = reg.source.checkpoint_schema.model_validate(cursor_raw)
        running = _RunningSource(
            registered=reg,
            cursor=cursor,
            save=self._save,
            log=self._log.bind(source_id=reg.source_id, source_type=reg.source.type),
        )
        running.log.info("streaming_runner.source_started")

        backoff = self._initial_backoff
        retries = 0
        reason = "disconnect"
        try:
            while not self._stopping:
                try:
                    await running.run_once()
                    reason = "disconnect"
                    break
                except asyncio.CancelledError:
                    reason = "cancelled"
                    raise
                except Exception as e:
                    retries += 1
                    if retries > self._max_retries:
                        running.log.error(
                            "streaming_runner.source_dead_letter",
                            retries=retries,
                            error=str(e),
                        )
                        reason = "dead_letter"
                        break
                    running.log.warning(
                        "streaming_runner.source_reconnect",
                        retry=retries,
                        backoff_s=backoff,
                        error=str(e),
                    )
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        reason = "cancelled"
                        raise
                    backoff = min(backoff * 2.0, self._max_backoff)
        finally:
            running.log.info("streaming_runner.source_stopped", reason=reason)
