"""Middleware chain — composición ortogonal de procesamiento por record.

El handler que el `StreamingRunner` le pasa a un `StreamingSource.listen()` es
una chain compuesta de `IngestMiddleware`s + un terminal. Cada middleware
recibe el record + el contexto + el `next` y decide:

  * llamar `await next(record)` para pasar al siguiente
  * NO llamar `next` para hacer short-circuit (drop)
  * mutar el record antes de pasarlo (rara vez deseable, los records son
    frozen — si necesitás mutar, construí uno nuevo)

Composición vs. herencia:

  * Cada concern (filter, gate, persist, métricas) vive en su propio
    middleware sin acoplarse a los otros.
  * Agregar lógica nueva (ej. `AIGateMiddleware` que valida con un LLM si
    procesar un mensaje instantáneo antes de persistirlo) es agregar UN
    middleware a la lista, no refactorizar el handler.
  * El orden de la chain importa: filtros primero, gates después, persist
    último.

Slots previstos (algunos implementados en este plan, otros documentados como
futuros):

  * `DeterministicFilterMiddleware` (Fase 1, en `memex.core.filters`) —
    aplica `filter_rules.action='ignore'` y hace drop puro.
  * `PersistMiddleware` (Fase 3, en `memex.core.ingest_middlewares`) —
    inserta en `inbox` vía `inbox.insert_record` y avanza el checkpoint.
    Es terminal (no llama `next`).
  * `AIGateMiddleware` (futuro, mencionado en plan) — para chats `streaming`
    con `priority`, manda el record a un LLM con un prompt validador que
    devuelve `NO_REPLY` o el texto a notificar. Si `NO_REPLY`, dropea. Si no,
    llama `next` (persist).

Observabilidad: cada middleware emite sus propios structlog events. El runner
no impone nada — el contrato es "haga algo útil con el record y opcionalmente
llame next". Counters globales viven en `memex.core.observability`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from memex.core.source import SourceRecord

Next = Callable[[SourceRecord], Awaitable[None]]
"""Continuación de la chain: el siguiente middleware (o el terminal)."""

Terminal = Callable[[SourceRecord], Awaitable[None]]
"""El último callable de la chain, sin `next` — típicamente PersistMiddleware
adaptado, o un sink que solo loggea en tests."""


class IngestContext(BaseModel):
    """Contexto compartido entre todos los middlewares de una invocación.

    Inmutable. Si un middleware necesita propagar info al siguiente, debe
    hacerlo vía atributos del record o vía side-effects controlados, no
    mutando este objeto.
    """

    source_id: int
    source_type: str
    user_id: int

    model_config = {"frozen": True}


@runtime_checkable
class IngestMiddleware(Protocol):
    """Pieza de la chain.

    Cada implementación define `async __call__(record, ctx, next)`:

      * Para pasar al siguiente: `await next(record)`.
      * Para hacer drop (short-circuit): no llamar `next`. Loguear es buena
        práctica para auditoría.
      * Para enriquecer el record: construir uno nuevo (los records son frozen)
        y pasarlo a `next`.

    NO debe mantener estado por-record entre invocaciones. El contexto vive
    en `ctx`; cualquier otro estado mutable debe estar protegido si la chain
    se invoca concurrentemente.
    """

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None: ...


def build_handler(
    middlewares: Sequence[IngestMiddleware],
    terminal: Terminal,
    ctx: IngestContext,
) -> Callable[[SourceRecord], Awaitable[None]]:
    """Componer una chain de middlewares + terminal en un handler invocable.

    Devuelve un `async (record) -> None` que el `StreamingRunner` puede pasarle
    al `StreamingSource.listen()`. Construir UNA vez por source (idealmente al
    startup); reutilizar para todos los events.

    Si `middlewares` está vacío, devuelve el terminal directamente.

    Orden de invocación: `middlewares[0]` recibe el record primero, decide si
    llamar `next` (el wrapper del siguiente middleware), y así hasta llegar al
    terminal.
    """

    async def call_terminal(record: SourceRecord) -> None:
        await terminal(record)

    handler: Callable[[SourceRecord], Awaitable[None]] = call_terminal

    for mw in reversed(middlewares):
        handler = _wrap(mw, handler, ctx)

    return handler


def _wrap(
    mw: IngestMiddleware,
    next_handler: Callable[[SourceRecord], Awaitable[None]],
    ctx: IngestContext,
) -> Callable[[SourceRecord], Awaitable[None]]:
    """Envolver un middleware con su `next` y el contexto.

    Capturados por closure para que la chain sea un solo callable que el
    listener invoca por evento.
    """

    async def wrapped(record: SourceRecord) -> None:
        await mw(record, ctx, next_handler)

    return wrapped
