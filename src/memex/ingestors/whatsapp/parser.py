"""De un mensaje de WhatsApp al `WhatsAppPayload`.

Dos orígenes con la misma forma lógica: el evento en vivo (`MessageEv`: `Info` + `Message`) y el
historial (`WebMessageInfo`: `key` + `message` + `messageTimestamp`). Ambos se normalizan primero a
`WaMessage` (quién, dónde, cuándo, el `waE2E.Message`) y de ahí sale el payload; la bandeja guarda
exactamente esos campos más el `Message` serializado, así que re-parsear desde la bandeja es el
mismo camino.

Qué NO es un mensaje del chat: stubs de sistema (alguien entró/salió, cambió el asunto), reacciones,
mensajes de protocolo (borrados, claves), estados (`status@broadcast`) y newsletters. Se devuelven
como `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from memex.core.payloads import WhatsAppPayload, WhatsAppSender

MediaKind = Literal["none", "photo", "video", "document", "audio", "voice", "sticker", "other"]

GROUP_SERVER = "g.us"
USER_SERVERS = ("s.whatsapp.net", "lid", "c.us")
IGNORED_SERVERS = ("broadcast", "newsletter")


@dataclass(frozen=True)
class WaMessage:
    """Forma normalizada de un mensaje, venga del vivo, del historial o de la bandeja."""

    chat_jid: str
    message_id: str
    from_me: bool
    sender: str  # número (o LID) de quien escribió; '' si no se sabe
    push_name: str
    occurred_at: datetime
    message: Any  # waE2E.Message (protobuf)
    #: True si `sender` es un LID (alias interno de WhatsApp) y no el número: el runtime lo
    #: resuelve al número con la sesión (`get_pn_from_lid`) para no partir a una persona en dos.
    sender_is_lid: bool = False


def jid_kind(jid: str) -> Literal["group", "user", "other"]:
    server = jid.rsplit("@", 1)[-1] if "@" in jid else ""
    if server == GROUP_SERVER:
        return "group"
    if server in USER_SERVERS:
        return "user"
    return "other"


def is_ignored_chat(jid: str) -> bool:
    server = jid.rsplit("@", 1)[-1] if "@" in jid else ""
    return server in IGNORED_SERVERS or jid.startswith("status@")


def _jid_str(jid: Any) -> str:
    user = str(getattr(jid, "User", "") or "")
    server = str(getattr(jid, "Server", "") or "")
    return f"{user}@{server}" if user and server else user


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if n <= 0:
        return datetime.now(UTC)
    if n > 10**12:  # milisegundos
        n //= 1000
    return datetime.fromtimestamp(n, UTC)


def from_live_event(event: Any) -> WaMessage:
    """`MessageEv` (neonize) → `WaMessage`."""
    info = event.Info
    src = info.MessageSource
    sender = str(getattr(src.Sender, "User", "") or "")
    is_lid = str(getattr(src.Sender, "Server", "") or "") == "lid"
    if is_lid:
        # Con direccionamiento por LID whatsmeow trae el número en `SenderAlt` cuando lo conoce.
        alt = str(getattr(getattr(src, "SenderAlt", None), "User", "") or "")
        if alt:
            sender, is_lid = alt, False
    return WaMessage(
        chat_jid=_jid_str(src.Chat),
        message_id=str(info.ID),
        from_me=bool(src.IsFromMe),
        sender=sender,
        push_name=str(getattr(info, "Pushname", "") or ""),
        occurred_at=_ts(getattr(info, "Timestamp", None)),
        message=event.Message,
        sender_is_lid=is_lid,
    )


def from_history_message(web_msg: Any, chat_jid: str) -> WaMessage:
    """`WebMessageInfo` de un `HistorySync` → `WaMessage`. En grupos `key.participant` es quien
    escribió; en chats 1:1 es el otro (o yo, si `fromMe`)."""
    key = web_msg.key
    from_me = bool(getattr(key, "fromMe", False))
    participant = str(getattr(key, "participant", "") or getattr(web_msg, "participant", "") or "")
    is_lid = False
    if participant:
        sender = participant.split("@", 1)[0]
        is_lid = participant.endswith("@lid")
    elif from_me:
        sender = ""
    else:
        sender = chat_jid.split("@", 1)[0] if jid_kind(chat_jid) == "user" else ""
        is_lid = chat_jid.endswith("@lid")
    return WaMessage(
        chat_jid=chat_jid,
        message_id=str(getattr(key, "ID", "") or ""),
        from_me=from_me,
        sender=sender,
        push_name=str(getattr(web_msg, "pushName", "") or ""),
        occurred_at=_ts(getattr(web_msg, "messageTimestamp", None)),
        message=web_msg.message,
        sender_is_lid=is_lid,
    )


def is_system_stub(web_msg: Any) -> bool:
    """Stubs del historial (entró/salió/cambió el asunto…): `messageStubType` distinto de 0."""
    return bool(getattr(web_msg, "messageStubType", 0))


def _has(msg: Any, field: str) -> bool:
    try:
        return bool(msg.HasField(field))
    except ValueError:
        return False


def _unwrap(msg: Any) -> Any:
    """Los mensajes efímeros / ver-una-vez / enviados desde otro dispositivo envuelven al real."""
    for wrapper in (
        "ephemeralMessage",
        "viewOnceMessage",
        "viewOnceMessageV2",
        "deviceSentMessage",
    ):
        if _has(msg, wrapper):
            inner = getattr(msg, wrapper).message
            return _unwrap(inner) if inner is not None else msg
    return msg


def media_kind_of(msg: Any) -> MediaKind:
    msg = _unwrap(msg)
    if _has(msg, "imageMessage"):
        return "photo"
    if _has(msg, "videoMessage"):
        return "video"
    if _has(msg, "documentMessage"):
        return "document"
    if _has(msg, "audioMessage"):
        return "voice" if bool(msg.audioMessage.PTT) else "audio"
    if _has(msg, "stickerMessage"):
        return "sticker"
    if (
        _has(msg, "locationMessage")
        or _has(msg, "contactMessage")
        or _has(msg, "liveLocationMessage")
    ):
        return "other"
    return "none"


def text_of(msg: Any) -> str:
    msg = _unwrap(msg)
    if msg.conversation:
        return str(msg.conversation)
    if _has(msg, "extendedTextMessage"):
        return str(msg.extendedTextMessage.text or "")
    for name in ("imageMessage", "videoMessage", "documentMessage"):
        if _has(msg, name):
            return str(getattr(msg, name).caption or "")
    if _has(msg, "locationMessage"):
        loc = msg.locationMessage
        return str(loc.name or loc.address or "")
    return ""


def _context_info(msg: Any) -> Any | None:
    msg = _unwrap(msg)
    for name in (
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "documentMessage",
        "audioMessage",
        "stickerMessage",
    ):
        if _has(msg, name):
            part = getattr(msg, name)
            if _has(part, "contextInfo"):
                return part.contextInfo
    return None


def is_content_message(msg: Any) -> bool:
    """¿Es algo que una persona dijo o mandó? Reacciones, protocolo y claves no lo son."""
    msg = _unwrap(msg)
    if _has(msg, "reactionMessage") or _has(msg, "protocolMessage"):
        return False
    if (
        _has(msg, "senderKeyDistributionMessage")
        and not text_of(msg)
        and media_kind_of(msg) == "none"
    ):
        return False
    return bool(text_of(msg)) or media_kind_of(msg) != "none"


def media_descriptor(msg: Any) -> tuple[str, str | None, int] | None:
    """(content_type, filename, size) del adjunto descargable, o None si no hay / no se baja
    (stickers, ubicaciones, contactos)."""
    msg = _unwrap(msg)
    for name, default_ext in (
        ("imageMessage", "jpg"),
        ("videoMessage", "mp4"),
        ("documentMessage", "bin"),
        ("audioMessage", "ogg"),
    ):
        if _has(msg, name):
            part = getattr(msg, name)
            mimetype = str(part.mimetype or "application/octet-stream").split(";")[0]
            filename = str(getattr(part, "fileName", "") or "") or None
            if filename is None:
                filename = f"{name.removesuffix('Message')}.{default_ext}"
            return mimetype, filename, int(part.fileLength or 0)
    return None


def build_payload(wm: WaMessage, *, chat_title: str) -> WhatsAppPayload | None:
    """`WaMessage` → payload. `None` si no es un mensaje con contenido o el chat se ignora."""
    if is_ignored_chat(wm.chat_jid) or not wm.message_id:
        return None
    msg = wm.message
    if msg is None or not is_content_message(msg):
        return None
    ctx = _context_info(msg)
    stanza = str(getattr(ctx, "stanzaID", "") or "") if ctx is not None else ""
    reply_to = stanza or None
    forwarded = bool(ctx is not None and getattr(ctx, "isForwarded", False))
    kind = media_kind_of(msg)
    text = text_of(msg)
    return WhatsAppPayload(
        chat_id=wm.chat_jid,
        chat_kind="group" if jid_kind(wm.chat_jid) == "group" else "user",
        chat_title=chat_title or None,
        message_id=wm.message_id,
        sender=WhatsAppSender(
            phone=wm.sender or None,
            display_name=wm.push_name or None,
            is_self=wm.from_me,
        ),
        date=wm.occurred_at,
        text=text,
        reply_to_message_id=reply_to,
        forwarded=forwarded,
        media_kind=kind,
        media_caption=text if kind != "none" and text else None,
    )


def external_id(wm: WaMessage) -> str:
    return f"whatsapp:{wm.chat_jid}:{wm.message_id}"
