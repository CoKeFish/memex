"""Pegamento con python-telegram-bot: convierte updates en `Incoming`, descarga fotos/notas de voz a
`media_dir` y manda las `Outgoing` (con botones inline). Transporte `polling` (getUpdates: funciona
detrás de NAT, sin puerto público) o `webhook` (URL pública + puerto: opción del VPS)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from memex.assistant.config import AssistantSettings
from memex.assistant.gateway import CANCEL, Gateway, Incoming, Outgoing, Sender
from memex.documentos.ingest import content_type_of
from memex.logging import get_logger

_log = get_logger("memex.assistant.telegram")

_MAX_MESSAGE = 4000
#: Marca de «sigo escribiendo» al final del texto parcial.
_CURSOR = " ▌"

_HELP = (
    "Soy el asistente de memex. Escribime en lenguaje natural, mandame una nota de voz o una "
    "foto (recibo, pantallazo).\n"
    "Consulto y respondo; las escrituras te las propongo y solo las ejecuto si confirmás.\n"
    "/cancelar descarta lo pendiente."
)


def _markup(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
    )


class TelegramStreamer:
    """Pinta la respuesta mientras llega: el primer `update` manda el mensaje, los siguientes lo
    EDITAN (a lo sumo cada `interval_s`, y solo si el texto cambió: la API de Telegram limita las
    ediciones); `finish` deja el texto final con los botones. Si nunca hubo texto parcial,
    `finish` devuelve False y el gateway manda la respuesta por la vía normal."""

    def __init__(self, bot: Any, chat_id: int, *, interval_s: float = 1.2) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._interval = interval_s
        self._message_id: int | None = None
        self._last_edit = 0.0
        self._shown = ""

    async def update(self, text: str) -> None:
        if not text.strip():
            return
        now = time.monotonic()
        partial = text[: _MAX_MESSAGE - len(_CURSOR)] + _CURSOR
        if self._message_id is None:
            try:
                msg = await self._bot.send_message(chat_id=self._chat_id, text=partial)
            except Exception:  # el streaming es cosmético: la respuesta final llega igual
                return
            self._message_id = int(msg.message_id)
            self._last_edit, self._shown = now, text
            return
        if text == self._shown or now - self._last_edit < self._interval:
            return
        await self._edit(partial, None)
        self._last_edit, self._shown = now, text

    async def finish(self, out: Outgoing) -> bool:
        if self._message_id is None:
            return False
        text = out.text or "(vacío)"
        chunks = [text[i : i + _MAX_MESSAGE] for i in range(0, len(text), _MAX_MESSAGE)]
        markup = _markup(out.buttons)
        await self._edit(chunks[0], markup if len(chunks) == 1 else None)
        for i, chunk in enumerate(chunks[1:], start=1):
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=chunk,
                reply_markup=markup if i == len(chunks) - 1 else None,
            )
        return True

    async def _edit(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        with contextlib.suppress(Exception):  # «message is not modified» y similares
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
                reply_markup=markup,
            )


def _entregar_documento(path: Path, nota: str) -> str:
    """Entrega el archivo a memex y lo procesa. Devuelve el texto de respuesta para el chat.

    Sync a propósito: `connection()` y el reproceso son bloqueantes; el caller lo corre en un hilo.
    """
    from memex.db import connection
    from memex.documentos.ingest import entregar_archivo
    from memex.documentos.procesar import procesar_documentos

    with connection() as conn:
        entrega = entregar_archivo(conn, 1, path, canal="telegram", nota=nota)
    if entrega.duplicado:
        return f"«{entrega.filename}» ya lo tenía; no lo dupliqué."
    if entrega.inbox_id is None:
        return f"«{entrega.filename}» quedó sin registrar."

    result = asyncio.run(procesar_documentos(1, [entrega.inbox_id]))
    stages = result.get("results", {})
    ocr = stages.get("ocr", {})
    extract = stages.get("extract", {})
    if int(ocr.get("errors", 0)):
        return (
            f"Guardé «{entrega.filename}» pero no pude leerlo. "
            "Si tiene contraseña, hay que cargarla en memex."
        )
    items = int(extract.get("items", 0))
    if not items:
        return f"Leí «{entrega.filename}» pero no saqué datos de él."
    return f"Listo: de «{entrega.filename}» saqué {items} dato(s)."


class TelegramBot:
    def __init__(self, settings: AssistantSettings, gateway: Gateway) -> None:
        self._settings = settings
        self._gateway = gateway
        self._media = settings.media_path
        self._media.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ salida
    def _sender(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Sender:
        async def send(out: Outgoing) -> None:
            markup = _markup(out.buttons)
            text = out.text or "(vacío)"
            chunks = [text[i : i + _MAX_MESSAGE] for i in range(0, len(text), _MAX_MESSAGE)]
            for i, chunk in enumerate(chunks):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_markup=markup if i == len(chunks) - 1 else None,
                )

        return send

    # ------------------------------------------------------------------ handlers
    async def _dispatch(self, inc: Incoming, context: ContextTypes.DEFAULT_TYPE) -> None:
        with contextlib.suppress(Exception):  # el «escribiendo…» es cosmético
            await context.bot.send_chat_action(chat_id=inc.chat_id, action=ChatAction.TYPING)
        send = self._sender(context, inc.chat_id)
        streamer = TelegramStreamer(
            context.bot, inc.chat_id, interval_s=self._settings.stream_edit_interval_s
        )
        try:
            await self._gateway.handle(inc, send, streamer)
        except Exception as e:
            _log.exception("assistant.telegram.turn_crashed", chat_id=inc.chat_id)
            await send(Outgoing(f"Se rompió algo procesando tu mensaje: {type(e).__name__}: {e}"))
        finally:
            for p in (*inc.images, *(x for x in (inc.audio,) if x is not None)):
                _cleanup(p)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        await context.bot.send_message(chat_id=update.effective_chat.id, text=_HELP)

    async def on_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None:
            return
        await self._dispatch(Incoming(chat.id, user.id, "callback", callback_data=CANCEL), context)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None or not msg.text:
            return
        await self._dispatch(Incoming(msg.chat_id, user.id, "text", text=msg.text), context)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None:
            return
        chat_dir = self._media / str(msg.chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        images: list[Path] = []
        if msg.photo:
            tg_file = await msg.photo[-1].get_file()  # la resolución más grande
            target = chat_dir / f"{uuid.uuid4().hex}.jpg"
            await tg_file.download_to_drive(str(target))
            images.append(target)
        elif msg.document is not None and (msg.document.mime_type or "").startswith("image/"):
            tg_file = await msg.document.get_file()
            suffix = Path(msg.document.file_name or "img.jpg").suffix or ".jpg"
            target = chat_dir / f"{uuid.uuid4().hex}{suffix}"
            await tg_file.download_to_drive(str(target))
            images.append(target)
        if not images:
            return
        await self._dispatch(
            Incoming(msg.chat_id, user.id, "photo", text=msg.caption or "", images=images), context
        )

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None:
            return
        attachment = msg.voice or msg.audio
        if attachment is None:
            return
        chat_dir = self._media / str(msg.chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        suffix = (
            ".ogg"
            if msg.voice is not None
            else Path(getattr(attachment, "file_name", "") or "a.mp3").suffix or ".mp3"
        )
        target = chat_dir / f"{uuid.uuid4().hex}{suffix}"
        tg_file = await attachment.get_file()
        await tg_file.download_to_drive(str(target))
        await self._dispatch(
            Incoming(msg.chat_id, user.id, "voice", text=msg.caption or "", audio=target), context
        )

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Un PDF/ZIP que el usuario le manda al bot: extracto, factura, contrato.

        Es la vía que funciona desde el teléfono y la que sigue sirviendo cuando memex viva en un
        VPS (una carpeta vigilada solo existe en la máquina de casa). El archivo entra por el mismo
        borde de ingesta que un adjunto de correo y se procesa en el acto, así la respuesta ya dice
        qué salió de él.
        """
        msg, user = update.effective_message, update.effective_user
        if msg is None or user is None or msg.document is None:
            return
        filename = msg.document.file_name or "documento.pdf"
        if content_type_of(filename) is None:
            await msg.reply_text(f"No sé leer «{filename}». Mandame un PDF, un ZIP o una imagen.")
            return

        chat_dir = self._media / str(msg.chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        target = chat_dir / f"{uuid.uuid4().hex}-{filename}"
        tg_file = await msg.document.get_file()
        await tg_file.download_to_drive(str(target))

        await msg.reply_text(f"Recibí «{filename}». Leyéndolo…")
        try:
            resumen = await asyncio.to_thread(_entregar_documento, target, msg.caption or "")
        except Exception as e:  # el usuario merece saber que falló
            _log.exception("assistant.telegram.document_failed", filename=filename)
            await msg.reply_text(f"No pude procesar «{filename}»: {e}")
            return
        await msg.reply_text(resumen)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or update.effective_chat is None or update.effective_user is None:
            return
        await query.answer()
        with contextlib.suppress(Exception):  # quitar los botones es cosmético
            await query.edit_message_reply_markup(reply_markup=None)
        await self._dispatch(
            Incoming(
                update.effective_chat.id,
                update.effective_user.id,
                "callback",
                callback_data=query.data or "",
            ),
            context,
        )

    # ------------------------------------------------------------------ arranque
    def build(self) -> Application:  # type: ignore[type-arg]
        # concurrent_updates: un botón apretado mientras corre un turno largo (una foto tarda
        # 40-60 s) se responde al instante en vez de esperar en la cola de Telegram y vencer
        # («Query is too old», 2026-09-17). El orden por chat lo sigue dando el lock del gateway.
        app = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .concurrent_updates(True)
            .build()
        )
        app.add_handler(CommandHandler(["start", "help", "ayuda"], self.on_start))
        app.add_handler(CommandHandler(["cancelar", "cancel"], self.on_cancel))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.on_photo))
        # Documentos que NO son imagen (PDF/ZIP): el orden importa, `Document.IMAGE` ya se
        # tomó las fotos mandadas como archivo.
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    def run(self) -> None:
        app = self.build()
        s = self._settings
        _log.info(
            "assistant.telegram.starting",
            transport=s.transport,
            allowed=len(s.allowed_ids),
            stt=s.stt,
        )
        if s.transport == "webhook":
            app.run_webhook(
                listen=s.webhook_listen,
                port=s.webhook_port,
                url_path="telegram",
                webhook_url=s.webhook_url.rstrip("/") + "/telegram",
                secret_token=s.webhook_secret or None,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
        else:
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


def _cleanup(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
