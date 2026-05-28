"""TelegramClientWrapper — fina capa sobre `telethon.TelegramClient`.

Encapsula:

- conexión / desconexión (async context manager)
- iteración incremental de mensajes por chat con `min_id` exclusivo
- iteración de dialogs para el comando `discover`
- helper sync que bridge-a una corrida async cuando el caller es sync (caso
  del runner polling)

NO contiene política de filtrado (allowlist, DMs) — eso vive en `parser.py` y
`source.py`. Es solo I/O Telethon.

Telethon es async-first; este wrapper acepta esa naturaleza y expone un
sync-bridge (`run_sync`) para usar desde `Source.fetch()` (que el contrato
exige sync).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from types import TracebackType
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.custom.dialog import Dialog
from telethon.tl.custom.message import Message

from memex.ingestors.telegram.config import TelegramConfig
from memex.logging import get_logger

EventCallback = Callable[[Any], Awaitable[None]]
"""Callback async para un evento Telethon. Recibe el `event` (proxy sobre el
Message); el caller resuelve `await event.get_chat()` / `get_sender()`."""


class TelegramClientWrapper:
    """Async context manager sobre `TelegramClient`.

    Uso:

        async with TelegramClientWrapper(cfg) as tc:
            async for msg in tc.iter_chat_messages(chat_id, min_id=42):
                ...

    El wrapper NO arranca el flujo interactivo de auth (`client.start()`); eso
    es responsabilidad del CLI `memex-telegram auth`. Si el session file está
    ausente o expirado, la conexión falla con un error claro en vez de
    bloquearse pidiendo SMS code.
    """

    def __init__(self, cfg: TelegramConfig, *, sequential_updates: bool = False) -> None:
        self.cfg = cfg
        # `sequential_updates=True` hace que Telethon procese los handlers de
        # update UNO A LA VEZ (en vez del default concurrente). Es obligatorio
        # para el listener streaming: el runner avanza el cursor por record y
        # handlers concurrentes lo correrían. El polling no usa update dispatch,
        # así que su default (False) es indiferente.
        self._sequential_updates = sequential_updates
        self._log = get_logger("memex.ingestors.telegram.client").bind(
            phone=cfg.phone_masked,
            session_name=cfg.session_name,
        )
        self._client: TelegramClient | None = None

    async def __aenter__(self) -> TelegramClientWrapper:
        # Telethon agrega ".session" al path automáticamente.
        # Asegurar que el directorio padre exista — Telethon NO lo crea.
        self.cfg.session_path.mkdir(parents=True, exist_ok=True)

        client = TelegramClient(
            str(self.cfg.session_file),
            self.cfg.api_id,
            self.cfg.api_hash,
            # Si Telegram nos rate-limita por menos de este threshold, Telethon
            # duerme y reintenta solo en vez de levantar FloodWaitError.
            # 60s cubre el caso común de bursts breves. Más allá lo dejamos
            # propagar — el supervisor del runner decide reintentar el batch.
            flood_sleep_threshold=60,
            sequential_updates=self._sequential_updates,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramAuthError("session is not authorized — run `memex-telegram auth` first")
        self._client = client
        self._log.info("telegram.client.connected")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                self._log.warning(
                    "telegram.client.disconnect_error",
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
            finally:
                self._client = None
                self._log.info("telegram.client.disconnected")

    def _require_client(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError("TelegramClientWrapper must be used as async context manager")
        return self._client

    async def get_me(self) -> Any:
        """Para health checks — confirma sesión válida devolviendo el user."""
        return await self._require_client().get_me()

    async def iter_chat_messages(
        self,
        chat_id: int,
        *,
        min_id: int,
        batch_size: int,
    ) -> AsyncIterator[Message]:
        """Yieldea mensajes con `id > min_id` en orden ascendente.

        `min_id` es EXCLUSIVO según el contrato de `client.iter_messages` —
        si el último visto fue id=100, pasar `min_id=100` yieldea desde 101.

        `reverse=True` garantiza orden oldest-first, que es lo que el runner
        espera para avanzar el cursor incrementalmente sin perder mensajes si
        el batch se interrumpe a mitad.
        """
        client = self._require_client()
        async for msg in client.iter_messages(
            chat_id,
            min_id=min_id,
            reverse=True,
            limit=batch_size,
        ):
            yield msg

    async def iter_dialogs(self) -> AsyncIterator[Dialog]:
        """Para `memex-telegram discover` — lista los chats accesibles."""
        client = self._require_client()
        async for dialog in client.iter_dialogs():
            yield dialog

    # ---- streaming (event-driven) ---- #

    def add_new_message_handler(self, callback: EventCallback, chat_ids: list[int]) -> None:
        """Registra `callback` para mensajes nuevos en `chat_ids`.

        `chat_ids` deben estar en formato marked (`get_peer_id`) — Telethon los
        toma tal cual sin resolución de red si son negativos. Registración
        programática vía `add_event_handler` (el decorator `@client.on` es solo
        azúcar sobre esto). El callback recibe el `event`; debe resolver
        `await event.get_chat()` / `get_sender()` antes de parsear porque en
        eventos live esas entidades pueden venir `None`/`min`.
        """
        client = self._require_client()
        client.add_event_handler(callback, events.NewMessage(chats=chat_ids))

    async def run_until_disconnected(self) -> None:
        """Bloquea hasta que el cliente se desconecte.

        Retorna LIMPIO (sin excepción) cuando `disconnect()` se llama desde
        otra task — exactamente lo que el `StreamingRunner.stop()` necesita.
        Solo propaga si hubo un error inesperado de update-handling.
        """
        client = self._require_client()
        await client.run_until_disconnected()

    async def disconnect(self) -> None:
        """Desconecta el cliente. Idempotente y seguro desde otra task.

        Llamarlo mientras `run_until_disconnected()` bloquea hace que ese await
        retorne limpio. Telethon cancela y espera los handlers en vuelo.
        """
        if self._client is not None:
            await self._client.disconnect()


class TelegramAuthError(RuntimeError):
    """Raised when the Telethon session is missing or unauthorized.

    Distinto de errores de red — este específicamente indica que el operador
    debe correr `memex-telegram auth` en una consola interactiva con SMS
    code para autorizar la session por primera vez (o re-autorizar si
    Telegram la invalidó).
    """


def run_sync[T](coro: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
    """Ejecuta una corrida async desde un caller sync.

    Wrapper sobre `asyncio.run` con dos garantías:
    1. Crea un nuevo event loop por invocación (no contamina estado entre
       fetches).
    2. Funciona también cuando se le pasa un `Awaitable` no-Coroutine
       (cualquier async function call envuelto en `coro = f()`).

    Importante: NO usar desde dentro de un event loop ya activo (Telethon en
    proceso async). Solo desde el runner sync de polling. Para streaming
    (Fase 3, lifespan de FastAPI), las APIs async se invocan directo.
    """
    if asyncio.iscoroutine(coro):
        result: T = asyncio.run(coro)
        return result
    # Si es Awaitable pero no Coroutine, envolverlo en una coroutine.

    async def _wrap() -> T:
        return await coro

    return asyncio.run(_wrap())
