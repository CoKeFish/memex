"""StreamingSource — contrato para fuentes event-driven (push, long-running).

Paralelo al Protocol `Source` de `memex.core.source` (que es polling, fetch-and-return).
Las dos abstracciones son hermanas, no jerárquicas: una fuente puede satisfacer
una, la otra, o ambas (ej. IMAP IDLE eventualmente puede pollearse Y escucharse).

Diferencia con `Source`:

  * `Source.fetch(checkpoint)` es síncrono y se invoca por ciclo desde un cron.
  * `StreamingSource.listen(handler)` es async y bloqueante — vive dentro de un
    task asyncio del `StreamingRunner` que el lifespan de FastAPI controla.

Contratos garantizados (enforced por mypy strict):

  * Genérico en `CursorT` — el cursor está tipado end-to-end, no `dict`.
  * `checkpoint_schema` obligatorio (mismo principio que `Source`).
  * `catchup(checkpoint)` es obligatorio: garantiza que un restart no pierde
    eventos. Drena cualquier item que haya ocurrido durante el downtime antes
    de empezar a escuchar.
  * `advance_checkpoint` con la misma firma que `Source` — el runner usa la
    misma lógica para persistir cursor entre records.
  * `listen(on_record)` recibe un handler externo (no inventa la lógica de
    qué hacer con el record). El handler es una chain compuesta por el runner
    vía `memex.core.middleware.build_handler` con `[DeterministicFilterMiddleware,
    PersistMiddleware, ...]`. Esto deja un slot explícito para insertar futuros
    middlewares (ej. `AIGateMiddleware` para validar mensajes instantáneos con
    LLM antes de persistir) sin tocar el Source.

Observabilidad esperada (cada implementación concreta debe emitir):

  * `streaming.connected` — cliente conectado al provider, antes de catchup.
  * `streaming.catchup` con `count` — drenaje offline terminado.
  * `streaming.event_received` por cada evento (sin metadata sensible).
  * `streaming.disconnected` — cierre limpio o por error.

El runner (`memex.core.streaming_runner.StreamingRunner`) supervisa el ciclo
de vida, reconecta con backoff exponencial si `listen()` falla y emite sus
propios events (`streaming_runner.*`).
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from memex.core.source import SourceRecord

CursorT = TypeVar("CursorT", bound=BaseModel)

StreamHandler = Callable[[SourceRecord], Awaitable[None]]
"""Lo que el `StreamingSource.listen` invoca por cada evento.

Construido por el runner vía `memex.core.middleware.build_handler`. Encapsula
el filter pre-ingest, la persistencia (vía `inbox.insert_record`) y el avance
de checkpoint. La fuente NO debe inventar este handler; lo recibe de afuera.
"""


@runtime_checkable
class StreamingSource(Protocol[CursorT]):
    """Fuente event-driven (push, long-running).

    Genérica en `CursorT` por las mismas razones que `Source` — el cursor está
    tipado y validado en la frontera por el runner, nunca llega como `dict` ni
    como `None`.
    """

    type: ClassVar[str]
    checkpoint_schema: ClassVar[_type[BaseModel]]

    def advance_checkpoint(self, checkpoint: CursorT, last: SourceRecord) -> CursorT:
        """Devolver el cursor actualizado después de procesar `last`.

        Misma firma que `Source.advance_checkpoint` para que el runner use la
        misma lógica de persistencia.
        """
        ...

    def catchup(self, checkpoint: CursorT) -> AsyncIterator[SourceRecord]:
        """Drenar lo que ocurrió mientras el listener estaba offline.

        Returns an async iterator (i.e. an `async def` containing `yield`).
        El runner itera, llama el handler chain por cada record, y avanza el
        checkpoint entre records — exactamente igual que el polling runner.

        Garantiza que un restart NO pierde eventos. Es obligatorio aunque
        devuelva 0 records.
        """
        ...

    async def listen(self, on_record: StreamHandler) -> None:
        """Escuchar eventos indefinidamente. Bloqueante hasta `disconnect()`.

        Por cada evento nuevo, `await on_record(record)`. El handler es opaco:
        la fuente no inspecciona qué hace, solo lo espera. Si el handler lanza,
        el runner registra el error y decide si reconectar.
        """
        ...

    async def disconnect(self) -> None:
        """Cerrar conexión limpiamente. Idempotente."""
        ...
