"""parse_telegram_message — DM rejection, chat_id normalization, topic_id, edge cases.

Usa fakes structural-typed para evitar instanciar Telethon types reales.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from telethon.tl.types import Channel, Chat, User

from memex.ingestors.telegram.parser import parse_telegram_message


@dataclass
class _FakeSender:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    bot: bool = False


@dataclass
class _FakeReplyTo:
    reply_to_msg_id: int | None = None
    reply_to_top_id: int | None = None
    forum_topic: bool = False


@dataclass
class _FakeForward:
    from_name: str | None = None
    from_id: Any = None


@dataclass
class _FakeMessage:
    id: int
    chat: Any
    date: datetime
    message: str = ""
    sender: _FakeSender | None = None
    reply_to: _FakeReplyTo | None = None
    forward: _FakeForward | None = None
    photo: Any = None
    video: Any = None
    audio: Any = None
    voice: Any = None
    sticker: Any = None
    document: Any = None
    text: str = ""

    @property
    def chat_id(self) -> int:
        return getattr(self.chat, "id", 0)

    # parser uses `msg.message` then `msg.text`
    extras: dict[str, Any] = field(default_factory=dict)


def _make_supergroup(chat_id: int, title: str = "Group") -> Channel:
    """Telethon Channel construido sin sus side-effects de cache."""
    return Channel(
        id=chat_id,
        title=title,
        photo=None,
        date=datetime(2026, 5, 28, tzinfo=UTC),
        megagroup=True,
    )


def _make_channel(chat_id: int, title: str = "News") -> Channel:
    return Channel(
        id=chat_id,
        title=title,
        photo=None,
        date=datetime(2026, 5, 28, tzinfo=UTC),
        broadcast=True,
    )


def _make_basic_group(chat_id: int, title: str = "Basic") -> Chat:
    return Chat(
        id=chat_id,
        title=title,
        photo=None,
        participants_count=2,
        date=datetime(2026, 5, 28, tzinfo=UTC),
        version=1,
    )


def _make_user(user_id: int) -> User:
    return User(id=user_id, first_name="Alice")


_MEDIA_KEYS = {"text", "photo", "video", "audio", "voice", "sticker", "document"}


def _msg(**overrides: Any) -> _FakeMessage:
    return _FakeMessage(
        id=overrides.pop("id", 100),
        chat=overrides.pop("chat", _make_supergroup(1234567890)),
        date=overrides.pop("date", datetime(2026, 5, 28, 10, 0, tzinfo=UTC)),
        message=overrides.pop("message", "hi"),
        sender=overrides.pop("sender", _FakeSender(id=99, first_name="Alice")),
        reply_to=overrides.pop("reply_to", None),
        forward=overrides.pop("forward", None),
        **{k: v for k, v in overrides.items() if k in _MEDIA_KEYS},
    )


# ---- DM rejection ---- #


def test_dm_chat_returns_none() -> None:
    """Mensajes de chats privados (User) NUNCA se persisten."""
    msg = _msg(chat=_make_user(7))
    assert parse_telegram_message(msg) is None


def test_unknown_chat_type_returns_none_fail_closed() -> None:
    """Fail-closed: si el tipo de peer no es User/Chat/Channel, NO se persiste.
    Si Telethon agrega un tipo nuevo, debe quedar fuera del store hasta que
    explícitamente se decida soportarlo."""

    class _UnknownPeer:
        id = 42
        title = "???"

    msg = _msg(chat=_UnknownPeer())
    assert parse_telegram_message(msg) is None


# ---- chat classification ---- #


def test_supergroup_parsed_as_supergroup() -> None:
    msg = _msg(chat=_make_supergroup(123, title="GroupName"))
    rec = parse_telegram_message(msg)
    assert rec is not None
    payload = rec.payload
    assert payload["chat_kind"] == "supergroup"
    assert payload["chat_title"] == "GroupName"


def test_broadcast_channel_parsed_as_channel() -> None:
    msg = _msg(chat=_make_channel(456))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["chat_kind"] == "channel"


def test_basic_group_parsed_as_group() -> None:
    msg = _msg(chat=_make_basic_group(789, title="Basic"))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["chat_kind"] == "group"


# ---- chat_id normalization ---- #


def test_chat_id_matches_telethon_get_peer_id_for_supergroup() -> None:
    """chat_id se normaliza vía telethon.utils.get_peer_id, formato 'marked'
    (`-(1e12 + id)` para Channel) — NO el legacy Bot-API `-100<id>` que usan
    bots como python-telegram-bot. Lo importante es que sea estable y que
    Telethon acepte el id de vuelta en iter_messages."""
    from telethon.utils import get_peer_id

    chat = _make_supergroup(1234567890)
    msg = _msg(chat=chat)
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["chat_id"] == get_peer_id(chat)


def test_chat_id_matches_telethon_get_peer_id_for_basic_group() -> None:
    """Basic groups → `-<id>` via get_peer_id."""
    from telethon.utils import get_peer_id

    chat = _make_basic_group(456)
    msg = _msg(chat=chat)
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["chat_id"] == get_peer_id(chat)
    assert rec.payload["chat_id"] == -456


# ---- topic_id ---- #


def test_topic_id_uses_reply_to_top_id_preferentially() -> None:
    """reply_to_top_id (topic root) gana sobre reply_to_msg_id (parent msg)."""
    msg = _msg(reply_to=_FakeReplyTo(reply_to_top_id=42, reply_to_msg_id=99))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["topic_id"] == 42


def test_topic_id_falls_back_to_msg_id_when_forum_topic_true() -> None:
    """Si forum_topic=True y no hay reply_to_top_id → el msg_id ES el root."""
    msg = _msg(reply_to=_FakeReplyTo(reply_to_msg_id=77, forum_topic=True))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["topic_id"] == 77


def test_topic_id_none_when_not_in_topic() -> None:
    msg = _msg(reply_to=None)
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["topic_id"] is None


def test_reply_to_message_id_skipped_when_equals_topic_id() -> None:
    """Si el message_id de reply == topic_id, no es "respuesta" sino root."""
    msg = _msg(reply_to=_FakeReplyTo(reply_to_msg_id=42, forum_topic=True))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["topic_id"] == 42
    assert rec.payload["reply_to_message_id"] is None


def test_reply_to_message_id_kept_when_different_from_topic() -> None:
    msg = _msg(reply_to=_FakeReplyTo(reply_to_msg_id=99, reply_to_top_id=42))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["topic_id"] == 42
    assert rec.payload["reply_to_message_id"] == 99


# ---- external_id + dedupe_keys ---- #


def test_external_id_shape_is_telegram_chatid_msgid() -> None:
    """external_id = `telegram:<normalized_chat_id>:<message_id>`."""
    from telethon.utils import get_peer_id

    chat = _make_supergroup(123)
    msg = _msg(id=42, chat=chat)
    rec = parse_telegram_message(msg)
    assert rec is not None
    expected = f"telegram:{get_peer_id(chat)}:42"
    assert rec.external_id == expected
    assert rec.dedupe_keys == [expected]


# ---- sender ---- #


def test_sender_built_from_first_and_last_name() -> None:
    msg = _msg(sender=_FakeSender(id=1, first_name="Ada", last_name="Lovelace"))
    rec = parse_telegram_message(msg)
    assert rec is not None
    sender = rec.payload["sender"]
    assert sender is not None
    assert sender["user_id"] == 1
    assert sender["display_name"] == "Ada Lovelace"


def test_sender_none_when_msg_has_no_sender() -> None:
    msg = _msg(sender=None)
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["sender"] is None


def test_sender_uses_title_when_no_name() -> None:
    """Anonymous channel posts often have only `title`."""
    msg = _msg(sender=_FakeSender(id=1, title="Newsroom"))
    rec = parse_telegram_message(msg)
    assert rec is not None
    sender = rec.payload["sender"]
    assert sender is not None
    assert sender["display_name"] == "Newsroom"


def test_sender_is_bot_flag_propagated() -> None:
    msg = _msg(sender=_FakeSender(id=1, first_name="Bot", bot=True))
    rec = parse_telegram_message(msg)
    assert rec is not None
    sender = rec.payload["sender"]
    assert sender is not None
    assert sender["is_bot"] is True


# ---- forward + media ---- #


def test_forward_from_name_captured() -> None:
    msg = _msg(forward=_FakeForward(from_name="Original Sender"))
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["forwarded_from"] == "Original Sender"


def test_media_kind_photo_with_caption() -> None:
    msg = _msg(message="my caption")
    msg.photo = object()  # truthy
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["media_kind"] == "photo"
    assert rec.payload["media_caption"] == "my caption"


def test_no_media_default() -> None:
    msg = _msg()
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["media_kind"] == "none"


# ---- defensive edges ---- #


def test_msg_without_id_returns_none() -> None:
    msg = _FakeMessage(id=0, chat=_make_supergroup(1), date=datetime(2026, 5, 28, tzinfo=UTC))
    assert parse_telegram_message(msg) is None


def test_msg_without_date_returns_none() -> None:
    msg = _msg()
    msg.date = None  # type: ignore[assignment]
    assert parse_telegram_message(msg) is None


def test_naive_date_assigned_utc() -> None:
    naive = datetime(2026, 5, 28, 10, 0)  # no tzinfo
    msg = _msg(date=naive)
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.occurred_at.tzinfo is not None


def test_text_field_empty_when_message_blank() -> None:
    msg = _msg(message="")
    rec = parse_telegram_message(msg)
    assert rec is not None
    assert rec.payload["text"] == ""
