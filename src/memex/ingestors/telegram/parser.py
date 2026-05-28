"""Parser: convierte un `telethon.Message` en un `SourceRecord` con `TelegramPayload`.

Reglas duras (defensa en profundidad):

1. **DMs nunca se persisten.** Si el chat es un `User` (chat privado 1:1),
   `parse_telegram_message` retorna `None`. La política de privacidad del
   proyecto excluye conversaciones personales del store; aunque alguien
   accidentalmente meta un DM en la allowlist, este parser lo descarta.

2. **chat_id normalizado vía `telethon.utils.get_peer_id`.** Telethon expone
   ids raw que difieren entre Channel/Chat/User; `get_peer_id` con
   `add_mark=True` (default) devuelve la forma "marked" estable:
   `-(1e12 + id)` para Channel/supergroup (NO el legacy Bot-API `-100<id>`),
   `-<id>` para grupos básicos, `<id>` para usuarios. Lo importante: ese id
   marcado se puede pasar de vuelta a `iter_messages` y Telethon lo entiende.
   Persistir SIN normalizar lleva a mismatches silenciosos contra la
   allowlist y a re-fetch de cada chat desde el principio.

3. **`topic_id` usa `reply_to_top_id`, NO `reply_to_msg_id`.** En foros
   (supergrupos con topics habilitados), `reply_to_msg_id` apunta al
   mensaje al que respondés *dentro* del topic; `reply_to_top_id` apunta al
   ROOT del topic, que es lo que la allowlist filtra. CoKeFish/ingestors
   confunde estos dos y eso rompería matching en foros.

4. **`external_id` formato**: `telegram:<chat_id>:<message_id>`. El runner
   usa `(source_id, external_id)` para dedupe, y `advance_checkpoint` lo
   parsea para recuperar `(chat_id, message_id)` sin tener que mirar el
   payload.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any, Literal

from telethon.tl.types import Channel, Chat, User
from telethon.utils import get_peer_id

from memex.core.payloads import TelegramPayload, TelegramSender
from memex.core.source import SourceRecord


def parse_telegram_message(msg: Any) -> SourceRecord | None:
    """Convierte un `telethon.tl.custom.message.Message` en `SourceRecord`.

    Retorna `None` si el mensaje debe descartarse (DM, sin chat, sin id, etc.).
    El caller debe filtrar `None` antes de yieldear.

    Acepta `Any` en la signature porque Telethon no exporta el tipo `Message`
    completo en estable; el contrato real es structural-typing.
    """
    chat = _get_attr(msg, "chat", None)
    if chat is None:
        return None

    chat_kind = _classify_chat(chat)
    if chat_kind is None or chat_kind == "dm":
        # Fail-closed: tipo desconocido o DM → no persistimos. La política de
        # privacidad excluye DMs y agregar un tipo nuevo de peer no debe
        # silenciosamente incluirlo en el store.
        return None

    message_id = _get_attr(msg, "id", None)
    if not isinstance(message_id, int) or message_id <= 0:
        return None

    chat_id = get_peer_id(chat)
    chat_title = _get_attr(chat, "title", None)

    occurred_at = _get_attr(msg, "date", None)
    if occurred_at is None:
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)

    topic_id = _extract_topic_id(msg)
    sender = _build_sender(msg)
    text = _get_attr(msg, "message", None) or _get_attr(msg, "text", None) or ""
    reply_to_message_id = _extract_reply_to_message_id(msg, topic_id)
    forwarded_from = _extract_forwarded_from(msg)
    media_kind, media_caption = _extract_media(msg)

    payload = TelegramPayload(
        chat_id=chat_id,
        chat_kind=chat_kind,
        chat_title=chat_title,
        topic_id=topic_id,
        message_id=message_id,
        sender=sender,
        date=occurred_at,
        text=text,
        reply_to_message_id=reply_to_message_id,
        forwarded_from=forwarded_from,
        media_kind=media_kind,
        media_caption=media_caption,
    )

    external_id = f"telegram:{chat_id}:{message_id}"
    return SourceRecord(
        external_id=external_id,
        occurred_at=occurred_at,
        payload=payload.model_dump(mode="json", by_alias=True),
        dedupe_keys=[external_id],
    )


def _classify_chat(chat: Any) -> Literal["dm", "group", "supergroup", "channel"] | None:
    """Clasifica el peer según el tipo Telethon.

    - `User`           → DM (rechazado por política)
    - `Chat`           → grupo básico (legacy)
    - `Channel.megagroup=True` → supergroup
    - `Channel.broadcast=True` → channel (broadcast)
    - cualquier otro tipo → `None` → fail-closed (NO se persiste)

    El fail-closed para tipos desconocidos es deliberado: si Telethon agrega
    un nuevo tipo de peer (ej. encriptado secreto), no queremos que aterrice
    silenciosamente en el store. El parser fuerza al operador a actualizar
    esta función explícitamente cuando aparece un tipo nuevo.
    """
    if isinstance(chat, User):
        return "dm"
    if isinstance(chat, Chat):
        return "group"
    if isinstance(chat, Channel):
        if getattr(chat, "megagroup", False):
            return "supergroup"
        return "channel"
    return None


def _extract_topic_id(msg: Any) -> int | None:
    """Devuelve el id del root del topic en foros, None si el chat no usa foros.

    Prefiere `reply_to_top_id` (el root del topic) sobre `reply_to_msg_id`
    (el mensaje específico al que respondés). Si el mensaje no es parte de
    un topic, retorna None.
    """
    reply_to = _get_attr(msg, "reply_to", None)
    if reply_to is None:
        return None
    top = _get_attr(reply_to, "reply_to_top_id", None)
    if isinstance(top, int) and top > 0:
        return top
    # Sin top_id: si forum_topic está flag-eado, el reply_to_msg_id ES el root.
    if _get_attr(reply_to, "forum_topic", False):
        raw = _get_attr(reply_to, "reply_to_msg_id", None)
        return int(raw) if isinstance(raw, int) and raw > 0 else None
    return None


def _extract_reply_to_message_id(msg: Any, topic_id: int | None) -> int | None:
    """Id del mensaje al que se está respondiendo, si lo hay.

    Si el message_id matchea el `topic_id`, devolvemos None — es el root
    del topic, no una respuesta a otro mensaje.
    """
    reply_to = _get_attr(msg, "reply_to", None)
    if reply_to is None:
        return None
    raw = _get_attr(reply_to, "reply_to_msg_id", None)
    if not isinstance(raw, int) or raw <= 0:
        return None
    if topic_id is not None and raw == topic_id:
        return None
    return raw


def _build_sender(msg: Any) -> TelegramSender | None:
    """Construye `TelegramSender` desde `msg.sender` o `msg.from_id`.

    Retorna None para service messages, posts de canal anónimos, etc.
    """
    sender = _get_attr(msg, "sender", None)
    if sender is None:
        return None
    user_id = _get_attr(sender, "id", None)
    if not isinstance(user_id, int):
        return None

    first = _get_attr(sender, "first_name", None) or ""
    last = _get_attr(sender, "last_name", None) or ""
    title = _get_attr(sender, "title", None)
    display_parts = [p for p in (first, last) if p]
    if display_parts:
        display_name: str | None = " ".join(display_parts)
    elif title:
        display_name = title
    else:
        display_name = None

    return TelegramSender(
        user_id=user_id,
        username=_get_attr(sender, "username", None),
        display_name=display_name,
        is_bot=bool(_get_attr(sender, "bot", False)),
    )


def _extract_forwarded_from(msg: Any) -> str | None:
    """Si el mensaje es un forward, devuelve un identificador legible.

    Telethon expone `msg.forward` con shape variable; intentamos extraer
    `from_name` (nombre original) o un peer id. None si no es forward.
    """
    fwd = _get_attr(msg, "forward", None)
    if fwd is None:
        return None
    from_name = _get_attr(fwd, "from_name", None)
    if from_name:
        return str(from_name)
    from_id = _get_attr(fwd, "from_id", None)
    if from_id is not None:
        return f"peer:{from_id}"
    return "unknown"


def _extract_media(
    msg: Any,
) -> tuple[
    Literal["none", "photo", "video", "document", "audio", "voice", "sticker", "other"], str | None
]:
    """Detecta el tipo de media presente. Retorna (kind, caption).

    Caption viene de `msg.message` cuando el mensaje es solo media + caption
    (sin texto). Por simplicidad retornamos la caption SIEMPRE que haya media
    — el caller decide si la duplica con `text`.
    """
    if _get_attr(msg, "photo", None) is not None:
        return ("photo", _get_attr(msg, "message", None))
    if _get_attr(msg, "video", None) is not None:
        return ("video", _get_attr(msg, "message", None))
    if _get_attr(msg, "audio", None) is not None:
        return ("audio", _get_attr(msg, "message", None))
    if _get_attr(msg, "voice", None) is not None:
        return ("voice", _get_attr(msg, "message", None))
    if _get_attr(msg, "sticker", None) is not None:
        return ("sticker", None)
    if _get_attr(msg, "document", None) is not None:
        return ("document", _get_attr(msg, "message", None))
    return ("none", None)


def _get_attr(obj: Any, name: str, default: Any) -> Any:
    """Defensive getattr — Telethon objects often raise on missing fields."""
    try:
        value = getattr(obj, name, default)
    except Exception:
        return default
    return value
