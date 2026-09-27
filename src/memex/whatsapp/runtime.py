"""El cliente de WhatsApp VIVO dentro del proceso del API (uno por cuenta vinculada).

Calca el lugar del streaming de Telegram: arranca en el lifespan para las cuentas ya vinculadas,
se reconecta solo (whatsmeow) y muere con el proceso. Vincular es a demanda (`pair`): se pide el
código para el número, se conecta, y cuando el dueño lo escribe en el teléfono llega `PairStatusEv`.

Flujo de un mensaje (vivo o del historial inicial):
1. se normaliza (`parser.WaMessage`) y se actualiza el CATÁLOGO del chat (nombre, cuántos, fechas);
2. si el chat está INCLUIDO (`allowed_chats`) y la fecha pasa el `since` → al inbox, con sus medios;
3. si no → a la BANDEJA cruda (sin bajar medios), acotada por retención.
Al cambiar la allowlist, `drain` vuelca la bandeja de los chats recién incluidos al inbox.

Todo el trabajo de DB corre en un hilo (`asyncio.to_thread`): los callbacks de neonize viven en el
event loop del API y no deben bloquearlo. neonize se importa tarde: el API y los tests no lo cargan
hasta que hace falta un cliente.

SOLO LECTURA: este módulo nunca envía mensajes, presencia ni recibos de lectura.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Connection

from memex.api.schemas import MediaItem
from memex.db import connection
from memex.ingestors.whatsapp.config import (
    DEFAULT_BUFFER_RETENTION_DAYS,
    DEFAULT_MAX_MEDIA_BYTES,
    WaAllowedChat,
    session_db_path,
)
from memex.ingestors.whatsapp.parser import (
    WaMessage,
    build_payload,
    from_history_message,
    from_live_event,
    is_ignored_chat,
    is_system_stub,
    jid_kind,
    media_descriptor,
)
from memex.logging import get_logger
from memex.whatsapp import store

_log = get_logger("memex.whatsapp.runtime")

#: Cuánto historial pedirle a WhatsApp al vincular (lo que mande es decisión de WhatsApp).
FULL_SYNC_DAYS = 730


@dataclass
class _MediaPending:
    """Un mensaje ya en el inbox al que le falta el archivo: se baja aparte, fuera del lock."""

    inbox_id: int
    wm: WaMessage


#: Bajadas de archivos en paralelo por cuenta al rellenar (historial/bandeja). Pocas a la vez: es
#: lo que haría WhatsApp Web al abrir un chat, no una ráfaga.
MEDIA_DOWNLOAD_CONCURRENCY = 3


@dataclass
class _Linked:
    """Una cuenta con su cliente neonize y su estado en memoria."""

    account_id: int
    user_id: int
    client: Any
    connected: bool = False
    source_id: int | None = None
    allowed: dict[str, WaAllowedChat] = field(default_factory=dict)
    #: nombres de chats conocidos (catálogo en memoria para rotular payloads sin ir a la DB).
    names: dict[str, str] = field(default_factory=dict)
    history_chunks: int = 0
    #: Tabla de nombres que manda WhatsApp aparte de los mensajes (el historial no trae el
    #: `pushName` por mensaje; los mensajes en vivo sí): `id (número o LID)` → nombre.
    pushnames: dict[str, str] = field(default_factory=dict)
    #: LID (alias interno de WhatsApp) → número, resuelto con la sesión y cacheado.
    lid_to_phone: dict[str, str] = field(default_factory=dict)
    #: Escritura en DB SERIALIZADA por cuenta: los chunks del historial inicial llegan en paralelo
    #: (varios handlers a la vez) y, cada uno upsertando los mismos chats en distinto orden, se
    #: bloqueaban entre sí (deadlocks en Postgres: se perdieron chunks en la primera vinculación
    #: real, 2026-09-13). Un mensaje o chunk a la vez por cuenta; los medios se bajan afuera.
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _build_client(db_path: str) -> Any:
    from neonize.aioze.client import NewAClient
    from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps

    props = DeviceProps(
        os="memex",
        platformType=DeviceProps.SAFARI,
        requireFullSync=True,
        historySyncConfig=DeviceProps.HistorySyncConfig(
            fullSyncDaysLimit=FULL_SYNC_DAYS,
            fullSyncSizeMbLimit=2000,
            storageQuotaMb=10000,
        ),
    )
    return NewAClient(db_path, props=props)


#: Cuánto esperar a que el cliente Go quede conectado tras `connect()` antes de pedir el código.
CLIENT_READY_TIMEOUT_S = 20.0


async def wait_client_ready(client: Any, *, timeout_s: float = CLIENT_READY_TIMEOUT_S) -> None:
    """`connect()` de neonize devuelve enseguida y conecta en segundo plano: `PairPhone` solo
    funciona cuando el cliente Go ya existe y abrió el socket. Sondea `is_connected`; si la tarea
    de conexión murió, propaga su error; si se agota el tiempo, `TimeoutError`."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        task = getattr(client, "connect_task", None)
        if task is not None and task.done():
            exc = task.exception()
            if exc is not None:
                raise exc
        try:
            # En el cliente ASINCRONO la propiedad devuelve una corrutina (la llamada a Go va a
            # un hilo): hay que esperarla; tomarla como valor la hace verdadera siempre.
            value: Any = client.is_connected
            if inspect.isawaitable(value):
                value = await value
            if bool(value):
                return
        except Exception:
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("WhatsApp no conectó a tiempo para pedir el código")
        await asyncio.sleep(0.25)


def _on(client: Any, event_cls: Any, handler: Callable[[Any, Any], Awaitable[None]]) -> None:
    """Registra un handler en el cliente neonize (su decorador no está tipado; acá sí)."""
    client.event(event_cls)(handler)


class WhatsAppRuntime:
    def __init__(self) -> None:
        self._linked: dict[int, _Linked] = {}
        self._lock = asyncio.Lock()
        self.max_media_bytes = DEFAULT_MAX_MEDIA_BYTES
        self.buffer_retention_days = DEFAULT_BUFFER_RETENTION_DAYS
        #: Rellenos de archivos en curso (segundo plano); se cancelan al apagar.
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ ciclo de vida
    async def start_linked(self) -> int:
        """Lifespan: conecta las cuentas ya vinculadas (sesión en disco). Nunca lanza."""
        try:
            rows = await asyncio.to_thread(self._linked_rows)
        except Exception as e:  # DB caída al boot: el API sigue sirviendo
            _log.warning("whatsapp.start.db_failed", error=str(e))
            return 0
        started = 0
        for account_id, user_id in rows:
            if not session_db_path(account_id).exists():
                _log.warning("whatsapp.start.session_missing", account_id=account_id)
                continue
            try:
                await self._connect(user_id, account_id)
                started += 1
            except Exception as e:  # best-effort por cuenta
                _log.warning("whatsapp.start.failed", account_id=account_id, error=str(e))
        if started:
            _log.info("whatsapp.start", accounts=started)
        return started

    @staticmethod
    def _linked_rows() -> list[tuple[int, int]]:
        with connection() as conn:
            return store.linked_accounts(conn)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for linked in list(self._linked.values()):
            try:
                await linked.client.disconnect()
            except Exception as e:
                _log.warning("whatsapp.stop.error", account_id=linked.account_id, error=str(e))
        self._linked.clear()

    def is_connected(self, account_id: int) -> bool:
        linked = self._linked.get(account_id)
        return bool(linked and linked.connected)

    def owns(self, account_id: int) -> bool:
        """True si el cliente de esa cuenta vive en ESTE proceso (el API). Desde otro proceso
        (daemon de notificaciones, consola) la conexión se lee del vínculo persistido, que este
        runtime mantiene al día (vinculado ↔ desconectado ↔ cerrado)."""
        return account_id in self._linked

    # ------------------------------------------------------------------ vincular
    async def pair(self, user_id: int, account_id: int, phone: str) -> str:
        """Pide el código de «Vincular con número de teléfono» y deja el cliente conectado
        esperando que el dueño lo escriba. Devuelve el código."""
        async with self._lock:
            existing = self._linked.get(account_id)
            if existing is not None:
                with contextlib.suppress(Exception):
                    await existing.client.disconnect()
                self._linked.pop(account_id, None)
            db = session_db_path(account_id)
            if db.exists():  # vínculo anterior: arrancar limpio
                db.unlink()
            linked = self._new_linked(user_id, account_id)
            # Orden que exige whatsmeow: primero CONECTAR (el cliente Go nace ahí y abre el
            # socket sin sesión), después pedir el código. Al revés responde «client is nil».
            await linked.client.connect()
            await wait_client_ready(linked.client)
            code = str(await linked.client.PairPhone(phone, show_push_notification=True))
            await asyncio.to_thread(
                self._db,
                lambda conn: store.upsert_link(
                    conn,
                    user_id,
                    account_id,
                    phone=phone,
                    status="codigo",
                    pair_code=code,
                    pair_code_at=datetime.now(UTC),
                    last_error=None,
                ),
            )
            self._linked[account_id] = linked
            _log.info("whatsapp.pair.code_issued", account_id=account_id)
            return code

    async def unlink(self, user_id: int, account_id: int) -> None:
        linked = self._linked.pop(account_id, None)
        if linked is not None:
            try:
                await linked.client.logout()
            except Exception as e:
                _log.warning("whatsapp.unlink.logout_failed", account_id=account_id, error=str(e))
            with contextlib.suppress(Exception):
                await linked.client.disconnect()
        db = session_db_path(account_id)
        if db.exists():
            db.unlink()
        await asyncio.to_thread(
            self._db,
            lambda conn: store.set_link_status(conn, account_id, "cerrado", jid=None),
        )
        _log.info("whatsapp.unlink", account_id=account_id)

    async def _connect(self, user_id: int, account_id: int) -> None:
        linked = self._new_linked(user_id, account_id)
        await linked.client.connect()
        self._linked[account_id] = linked

    def _new_linked(self, user_id: int, account_id: int) -> _Linked:
        db = session_db_path(account_id)
        db.parent.mkdir(parents=True, exist_ok=True)
        client = _build_client(str(db))
        linked = _Linked(account_id=account_id, user_id=user_id, client=client)
        self._register_handlers(linked)
        return linked

    # ------------------------------------------------------------------ allowlist
    async def reload_allowed(self, account_id: int) -> list[str]:
        """Relee la allowlist de la fuente; devuelve los JIDs que pasaron a estar incluidos."""
        linked = self._linked.get(account_id)
        if linked is None:
            return []
        before = set(linked.allowed)
        await asyncio.to_thread(self._refresh_allowed, linked)
        return sorted(set(linked.allowed) - before)

    def _refresh_allowed(self, linked: _Linked) -> None:
        with connection() as conn:
            src = store.ensure_source(conn, linked.user_id, linked.account_id)
            linked.source_id = int(src["id"])
            linked.allowed = store.load_allowed(conn, linked.source_id)

    async def drain(self, user_id: int, account_id: int, chat_jids: list[str]) -> int:
        """Vuelca la bandeja de `chat_jids` (recién incluidos) al inbox. El texto entra acá mismo;
        los archivos (la bandeja guarda las llaves, no los bytes) se bajan en segundo plano con el
        cliente vivo y se adjuntan a cada mensaje. Sin cliente vivo entra solo el texto."""
        linked = self._linked.get(account_id)
        total = 0
        pending: list[_MediaPending] = []
        lock = linked.db_lock if linked is not None else asyncio.Lock()
        async with lock:
            for jid in chat_jids:
                n, p = await asyncio.to_thread(self._drain_chat, user_id, account_id, jid, linked)
                total += n
                pending.extend(p)
        if total:
            _log.info(
                "whatsapp.drain",
                account_id=account_id,
                chats=len(chat_jids),
                ingested=total,
                media_pending=len(pending),
            )
        if linked is not None:
            self._spawn_media_backfill(linked, pending)
            await self.refresh_descriptions(linked, chat_jids)
        return total

    def _drain_chat(
        self, user_id: int, account_id: int, jid: str, linked: _Linked | None
    ) -> tuple[int, list[_MediaPending]]:
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        with connection() as conn:
            src = store.ensure_source(conn, user_id, account_id)
            source_id = int(src["id"])
            allowed = store.load_allowed(conn, source_id).get(jid)
            if allowed is None:
                return 0, []
            name = store.chat_name(conn, account_id, jid)
            rows = store.buffer_drain(conn, account_id, jid)
            n = 0
            pending: list[_MediaPending] = []
            description = store.chat_description(conn, account_id, jid)
            if description:
                got = store.ingest_description(
                    conn,
                    user_id,
                    source_id,
                    jid=jid,
                    name=name,
                    description=description,
                    when=datetime.now(UTC),
                )
                if got.inserted:
                    n += 1
            for r in rows:
                msg = Message()
                msg.ParseFromString(bytes(r["raw"]))
                wm = WaMessage(
                    chat_jid=jid,
                    message_id=str(r["message_id"]),
                    from_me=bool(r["from_me"]),
                    sender=str(r["sender"] or ""),
                    push_name=str(r["push_name"] or ""),
                    occurred_at=r["occurred_at"],
                    message=msg,
                )
                if not allowed.admits(wm.occurred_at.date()):
                    continue
                payload = build_payload(wm, chat_title=name)
                if payload is None:
                    continue
                got = store.ingest(conn, user_id, source_id, wm, payload, media=[])
                if got.inserted:
                    n += 1
                p = _pending_media(conn, wm, got)
                if p is not None:
                    pending.append(p)
            if linked is not None:
                linked.allowed = store.load_allowed(conn, source_id)
            return n, pending

    # ------------------------------------------------------------------ eventos
    def _register_handlers(self, linked: _Linked) -> None:
        from neonize.aioze.events import (
            ConnectedEv,
            ConnectFailureEv,
            DisconnectedEv,
            HistorySyncEv,
            LoggedOutEv,
            MessageEv,
            PairStatusEv,
            TemporaryBanEv,
        )

        client = linked.client
        account_id, user_id = linked.account_id, linked.user_id

        async def on_pair(_: Any, ev: Any) -> None:
            jid = str(getattr(getattr(ev, "ID", None), "User", "") or "")
            await asyncio.to_thread(
                self._db,
                lambda conn: store.upsert_link(
                    conn,
                    user_id,
                    account_id,
                    status="vinculado",
                    jid=jid or None,
                    linked_at=datetime.now(UTC),
                    pair_code=None,
                    last_error=None,
                ),
            )
            _log.info("whatsapp.paired", account_id=account_id)

        async def on_connected(_: Any, __: Any) -> None:
            linked.connected = True
            await asyncio.to_thread(self._on_connected_db, linked)
            _log.info("whatsapp.connected", account_id=account_id)
            await self.refresh_descriptions(linked, list(linked.allowed))

        async def on_disconnected(_: Any, __: Any) -> None:
            linked.connected = False
            await asyncio.to_thread(
                self._db, lambda conn: store.set_link_status(conn, account_id, "desconectado")
            )

        async def on_logged_out(_: Any, ev: Any) -> None:
            linked.connected = False
            await asyncio.to_thread(
                self._db,
                lambda conn: store.set_link_status(
                    conn, account_id, "cerrado", last_error=str(getattr(ev, "Reason", ""))
                ),
            )
            _log.warning("whatsapp.logged_out", account_id=account_id)

        async def on_ban(_: Any, ev: Any) -> None:
            linked.connected = False
            detail = f"code={getattr(ev, 'Code', '')} expire={getattr(ev, 'Expire', '')}"
            await asyncio.to_thread(
                self._db,
                lambda conn: store.set_link_status(conn, account_id, "baneado", last_error=detail),
            )
            _log.error("whatsapp.temporary_ban", account_id=account_id, detail=detail)

        async def on_fail(_: Any, ev: Any) -> None:
            detail = f"{getattr(ev, 'Reason', '')} {getattr(ev, 'Message', '')}".strip()
            await asyncio.to_thread(
                self._db, lambda conn: store.set_link_error(conn, account_id, detail)
            )
            _log.warning("whatsapp.connect_failure", account_id=account_id, detail=detail)

        async def on_history(_: Any, ev: Any) -> None:
            linked.history_chunks += 1
            try:
                _absorb_pushnames(linked, ev.Data)
                await self._resolve_history_lids(linked, ev.Data)
                async with linked.db_lock:
                    stats, pending = await asyncio.to_thread(
                        self._ingest_history_chunk, linked, ev.Data
                    )
            except Exception as e:
                _log.warning("whatsapp.history.error", account_id=account_id, error=str(e))
                return
            if pending:
                stats["media_pending"] = len(pending)
                self._spawn_media_backfill(linked, pending)
            _log.info("whatsapp.history.chunk", account_id=account_id, **stats)

        async def on_message(_: Any, ev: Any) -> None:
            try:
                wm = from_live_event(ev)
                if is_ignored_chat(wm.chat_jid):
                    return
                wm = await self._with_identity(linked, wm)
                media = await self._download_if_included(linked, wm)
                async with linked.db_lock:
                    await asyncio.to_thread(self._ingest_one, linked, wm, media)
            except Exception as e:
                _log.warning("whatsapp.message.error", account_id=account_id, error=str(e))

        _on(client, PairStatusEv, on_pair)
        _on(client, ConnectedEv, on_connected)
        _on(client, DisconnectedEv, on_disconnected)
        _on(client, LoggedOutEv, on_logged_out)
        _on(client, TemporaryBanEv, on_ban)
        _on(client, ConnectFailureEv, on_fail)
        _on(client, HistorySyncEv, on_history)
        _on(client, MessageEv, on_message)

    # ------------------------------------------------------------------ trabajo en DB (hilo)
    @staticmethod
    def _db(fn: Any) -> None:
        with connection() as conn:
            fn(conn)

    def _on_connected_db(self, linked: _Linked) -> None:
        with connection() as conn:
            store.set_link_status(conn, linked.account_id, "vinculado", last_error=None)
            src = store.ensure_source(conn, linked.user_id, linked.account_id)
            linked.source_id = int(src["id"])
            linked.allowed = store.load_allowed(conn, linked.source_id)
            linked.names = store.chat_names(conn, linked.account_id)
            purged = store.buffer_purge(conn, linked.account_id, self.buffer_retention_days)
        if purged:
            _log.info("whatsapp.buffer.purged", account_id=linked.account_id, rows=purged)

    def _ingest_history_chunk(
        self, linked: _Linked, data: Any
    ) -> tuple[dict[str, Any], list[_MediaPending]]:
        """Un chunk de historial: catálogo + (inbox si incluido | bandeja). Devuelve las
        estadísticas y los mensajes incluidos a los que les falta el archivo (se baja aparte)."""
        sync_type = data.HistorySyncType.Name(data.syncType) if data.HasField("syncType") else "?"
        conversations = 0
        ingested = buffered = skipped = 0
        pending: list[_MediaPending] = []
        with connection() as conn:
            if linked.source_id is None:
                src = store.ensure_source(conn, linked.user_id, linked.account_id)
                linked.source_id = int(src["id"])
                linked.allowed = store.load_allowed(conn, linked.source_id)
            # Orden estable por JID: aun serializado, tocar las filas siempre en el mismo orden
            # evita bloqueos cruzados con cualquier otra escritura concurrente (el drain, la UI).
            for conv in sorted(data.conversations, key=lambda c: str(c.ID)):
                jid = str(conv.ID)
                if is_ignored_chat(jid):
                    continue
                conversations += 1
                name = str(conv.name or getattr(conv, "displayName", "") or "")
                if name:
                    linked.names[jid] = name
                msgs: list[WaMessage] = []
                for hm in conv.messages:
                    web_msg = hm.message
                    if is_system_stub(web_msg):
                        continue
                    msgs.append(_named(linked, from_history_message(web_msg, jid)))
                times = [m.occurred_at for m in msgs]
                description = str(getattr(conv, "description", "") or "").strip()
                store.upsert_chat(
                    conn,
                    linked.user_id,
                    linked.account_id,
                    jid,
                    name=name,
                    kind=jid_kind(jid),
                    new_messages=len(msgs),
                    first_at=min(times) if times else None,
                    last_at=max(times) if times else None,
                    description=description or None,
                )
                allowed = linked.allowed.get(jid)
                if allowed is not None and description:
                    got = store.ingest_description(
                        conn,
                        linked.user_id,
                        linked.source_id,
                        jid=jid,
                        name=linked.names.get(jid, name),
                        description=description,
                        when=max(times) if times else datetime.now(UTC),
                    )
                    if got.inserted:
                        ingested += 1
                for wm in msgs:
                    if allowed is not None:
                        if not allowed.admits(wm.occurred_at.date()):
                            skipped += 1
                            continue
                        payload = build_payload(wm, chat_title=linked.names.get(jid, name))
                        if payload is None:
                            skipped += 1
                            continue
                        got = store.ingest(conn, linked.user_id, linked.source_id, wm, payload, [])
                        if got.inserted:
                            ingested += 1
                        p = _pending_media(conn, wm, got)
                        if p is not None:
                            pending.append(p)
                    else:
                        if store.buffer_put(conn, linked.user_id, linked.account_id, wm):
                            buffered += 1
        stats: dict[str, Any] = {
            "sync_type": sync_type,
            "conversations": conversations,
            "ingested": ingested,
            "buffered": buffered,
            "skipped": skipped,
        }
        return stats, pending

    def _ingest_one(self, linked: _Linked, wm: WaMessage, media: list[MediaItem]) -> None:
        with connection() as conn:
            if linked.source_id is None:
                src = store.ensure_source(conn, linked.user_id, linked.account_id)
                linked.source_id = int(src["id"])
                linked.allowed = store.load_allowed(conn, linked.source_id)
            name = linked.names.get(wm.chat_jid) or (
                wm.push_name if jid_kind(wm.chat_jid) == "user" else ""
            )
            if name and wm.chat_jid not in linked.names:
                linked.names[wm.chat_jid] = name
            store.upsert_chat(
                conn,
                linked.user_id,
                linked.account_id,
                wm.chat_jid,
                name=name,
                kind=jid_kind(wm.chat_jid),
                new_messages=1,
                first_at=wm.occurred_at,
                last_at=wm.occurred_at,
            )
            allowed = linked.allowed.get(wm.chat_jid)
            if allowed is None:
                store.buffer_put(conn, linked.user_id, linked.account_id, wm)
                return
            if not allowed.admits(wm.occurred_at.date()):
                return
            payload = build_payload(wm, chat_title=name)
            if payload is None:
                return
            got = store.ingest(conn, linked.user_id, linked.source_id, wm, payload, media)
            if not got.inserted and media and got.inbox_id is not None:
                # Ya estaba (p. ej. entró por el historial sin el archivo): se le adjunta ahora.
                store.attach_media(conn, linked.user_id, got.inbox_id, media)

    # ------------------------------------------------------------------ nombres y LIDs
    async def _resolve_lid(self, linked: _Linked, lid: str) -> str:
        """LID → número vía la sesión (`get_pn_from_lid`), cacheado; si no se conoce, queda el
        LID (mejor un identificador estable que nada)."""
        cached = linked.lid_to_phone.get(lid)
        if cached is not None:
            return cached
        phone = lid
        try:
            from neonize.utils.jid import build_jid

            # build_jid rellena los campos obligatorios del proto (RawAgent, Device, Integrator);
            # JID(User=..., Server=...) a secas falla: «missing required fields» (2026-09-13).
            res = await linked.client.get_pn_from_lid(build_jid(lid, "lid"))
            user = str(getattr(res, "User", "") or "")
            if user:
                phone = user
        except Exception as e:
            _log.info("whatsapp.lid.unresolved", account_id=linked.account_id, error=str(e))
        linked.lid_to_phone[lid] = phone
        return phone

    async def _resolve_history_lids(self, linked: _Linked, data: Any) -> None:
        """Resuelve (una vez) los LIDs que aparecen en el chunk, antes del trabajo en DB."""
        lids: set[str] = set()
        for conv in data.conversations:
            jid = str(conv.ID)
            if jid.endswith("@lid"):
                lids.add(jid.split("@", 1)[0])
            for hm in conv.messages:
                key = hm.message.key
                part = str(getattr(key, "participant", "") or "")
                if part.endswith("@lid"):
                    lids.add(part.split("@", 1)[0])
        for lid in lids:
            if lid not in linked.lid_to_phone:
                await self._resolve_lid(linked, lid)

    async def _with_identity(self, linked: _Linked, wm: WaMessage) -> WaMessage:
        """Mensaje en vivo: LID → número si hace falta; el nombre que trae alimenta la tabla."""
        if wm.sender_is_lid and wm.sender:
            wm = replace(wm, sender=await self._resolve_lid(linked, wm.sender), sender_is_lid=False)
        if wm.push_name and wm.sender:
            linked.pushnames[wm.sender] = wm.push_name
        return _named(linked, wm)

    # ------------------------------------------------------------------ descripción del grupo
    async def refresh_descriptions(self, linked: _Linked, chat_jids: list[str]) -> int:
        """Lee con la sesión viva la descripción (tema) actual de los GRUPOS incluidos y la guarda;
        una descripción nueva entra al inbox como mensaje del grupo. Best-effort por chat. Devuelve
        cuántas entraron nuevas."""
        if not linked.connected:
            return 0
        from neonize.utils.jid import build_jid

        ingested = 0
        for jid in chat_jids:
            if jid not in linked.allowed or jid_kind(jid) != "group":
                continue
            try:
                info = await linked.client.get_group_info(build_jid(jid.split("@", 1)[0], "g.us"))
            except Exception as e:
                _log.info(
                    "whatsapp.group_info.failed",
                    account_id=linked.account_id,
                    jid=jid,
                    error=str(e),
                )
                continue
            topic = getattr(info, "GroupTopic", None)
            description = str(getattr(topic, "Topic", "") or "").strip()
            set_at = int(getattr(topic, "TopicSetAt", 0) or 0)
            when = datetime.fromtimestamp(set_at, tz=UTC) if set_at > 0 else datetime.now(UTC)
            async with linked.db_lock:
                got = await asyncio.to_thread(
                    self._save_description, linked, jid, description, when
                )
            if got:
                ingested += 1
        if ingested:
            _log.info(
                "whatsapp.descriptions.refreshed", account_id=linked.account_id, ingested=ingested
            )
        return ingested

    def _save_description(
        self, linked: _Linked, jid: str, description: str, when: datetime
    ) -> bool:
        with connection() as conn:
            if linked.source_id is None:
                src = store.ensure_source(conn, linked.user_id, linked.account_id)
                linked.source_id = int(src["id"])
            store.upsert_chat(
                conn,
                linked.user_id,
                linked.account_id,
                jid,
                name=linked.names.get(jid, ""),
                kind=jid_kind(jid),
                new_messages=0,
                first_at=None,
                last_at=None,
                description=description,
            )
            if not description:
                return False
            got = store.ingest_description(
                conn,
                linked.user_id,
                linked.source_id,
                jid=jid,
                name=linked.names.get(jid, ""),
                description=description,
                when=when,
            )
            return bool(got.inserted)

    # ------------------------------------------------------------------ medios
    def _spawn_media_backfill(self, linked: _Linked, pending: list[_MediaPending]) -> None:
        """Los archivos se bajan en segundo plano: no frenan la respuesta de /chats ni el siguiente
        chunk del historial, y el lock de DB se toma solo el instante de adjuntar cada uno."""
        if not pending:
            return
        task = asyncio.create_task(self._backfill_media(linked, pending))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _backfill_media(
        self, linked: _Linked, pending: list[_MediaPending]
    ) -> dict[str, int]:
        """Baja el archivo de cada mensaje pendiente (pocas a la vez) y lo adjunta a su fila. Lo
        que WhatsApp ya expiró en sus servidores falla y queda sin archivo (se cuenta)."""
        stats = {"requested": len(pending), "attached": 0, "failed": 0}
        if not linked.connected:
            _log.info("whatsapp.media.backfill.offline", account_id=linked.account_id, **stats)
            return stats
        sem = asyncio.Semaphore(MEDIA_DOWNLOAD_CONCURRENCY)

        async def one(p: _MediaPending) -> None:
            try:
                async with sem:
                    media = await self._download_if_included(linked, p.wm)
                if not media:
                    stats["failed"] += 1
                    return
                async with linked.db_lock:
                    n = await asyncio.to_thread(self._attach_media, linked, p.inbox_id, media)
                stats["attached" if n else "failed"] += 1
            except Exception as e:
                stats["failed"] += 1
                _log.warning(
                    "whatsapp.media.backfill.error",
                    account_id=linked.account_id,
                    inbox_id=p.inbox_id,
                    error=str(e),
                )

        await asyncio.gather(*(one(p) for p in pending))
        _log.info("whatsapp.media.backfill", account_id=linked.account_id, **stats)
        return stats

    def _attach_media(self, linked: _Linked, inbox_id: int, media: list[MediaItem]) -> int:
        with connection() as conn:
            return store.attach_media(conn, linked.user_id, inbox_id, media)

    async def _download_if_included(self, linked: _Linked, wm: WaMessage) -> list[MediaItem]:
        """Baja el adjunto SOLO si el chat está incluido (la bandeja no guarda medios)."""
        if wm.chat_jid not in linked.allowed:
            return []
        desc = media_descriptor(wm.message)
        if desc is None:
            return []
        content_type, filename, size = desc
        if size and size > self.max_media_bytes:
            _log.info("whatsapp.media.too_big", account_id=linked.account_id, size=size)
            return []
        try:
            data = await linked.client.download_any(wm.message)
        except Exception as e:
            _log.info("whatsapp.media.download_failed", account_id=linked.account_id, error=str(e))
            return []
        if not data:
            return []
        if len(data) > self.max_media_bytes:
            return []
        return [
            MediaItem(
                sha256=hashlib.sha256(data).hexdigest(),
                content_type=content_type,
                filename=filename,
                size=len(data),
                data_b64=base64.b64encode(data).decode("ascii"),
            )
        ]


def _pending_media(conn: Connection, wm: WaMessage, got: store.Ingested) -> _MediaPending | None:
    """Si el mensaje trae un adjunto y su fila del inbox aún no lo tiene, queda pendiente de bajar.
    Cubre al mensaje nuevo (historial/bandeja: el texto viene con las llaves del archivo, no con
    los bytes) y al que ya estaba sin archivo (una re-vinculación trae el historial otra vez)."""
    if got.inbox_id is None or media_descriptor(wm.message) is None:
        return None
    if store.has_media(conn, got.inbox_id):
        return None
    return _MediaPending(inbox_id=got.inbox_id, wm=wm)


def _absorb_pushnames(linked: _Linked, data: Any) -> int:
    """La tabla `pushnames` del historial (`id → nombre`), clave por la parte de usuario del JID
    (número o LID). Devuelve cuántos nombres nuevos aprendió."""
    n = 0
    for p in getattr(data, "pushnames", ()):
        jid = str(getattr(p, "ID", "") or "")
        name = str(getattr(p, "pushname", "") or "").strip()
        if not jid or not name:
            continue
        user = jid.split("@", 1)[0]
        if linked.pushnames.get(user) != name:
            linked.pushnames[user] = name
            n += 1
    return n


def _named(linked: _Linked, wm: WaMessage) -> WaMessage:
    """Completa remitente y nombre con lo que la cuenta ya sabe: LID → número (cache) y nombre
    desde la tabla de pushnames (por número o por LID)."""
    lid = wm.sender if wm.sender_is_lid else ""
    sender = linked.lid_to_phone.get(wm.sender, wm.sender) if wm.sender_is_lid else wm.sender
    name = (
        wm.push_name
        or linked.pushnames.get(sender, "")
        or (linked.pushnames.get(lid, "") if lid else "")
    )
    if sender == wm.sender and name == wm.push_name:
        return wm
    return replace(
        wm, sender=sender, push_name=name, sender_is_lid=wm.sender_is_lid and sender == lid
    )


#: Instancia única del proceso (lifespan del API + router).
runtime = WhatsAppRuntime()
