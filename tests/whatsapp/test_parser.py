"""Parser de WhatsApp: de los protobufs de whatsmeow al payload (sin red ni DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.proto.waWeb.WAWebProtobufsWeb_pb2 import WebMessageInfo

from memex.ingestors.whatsapp.parser import (
    WaMessage,
    build_payload,
    external_id,
    from_history_message,
    is_content_message,
    is_ignored_chat,
    is_system_stub,
    jid_kind,
    media_descriptor,
    media_kind_of,
    text_of,
)

GROUP = "120363000000000001@g.us"
USER = "573001112233@s.whatsapp.net"


def _wm(msg: Message, *, chat: str = GROUP, from_me: bool = False) -> WaMessage:
    return WaMessage(
        chat_jid=chat,
        message_id="3EB0ABCDEF",
        from_me=from_me,
        sender="573009998877",
        push_name="Ana",
        occurred_at=datetime(2026, 9, 1, 15, 30, tzinfo=UTC),
        message=msg,
    )


def test_jid_kinds_and_ignored_chats() -> None:
    assert jid_kind(GROUP) == "group"
    assert jid_kind(USER) == "user"
    assert jid_kind("123@lid") == "user"
    assert jid_kind("algo@newsletter") == "other"
    assert is_ignored_chat("status@broadcast")
    assert is_ignored_chat("123@newsletter")
    assert not is_ignored_chat(GROUP)


def test_plain_text_message_becomes_payload() -> None:
    msg = Message(conversation="Mañana 7pm en la cancha")
    payload = build_payload(_wm(msg), chat_title="Fútbol jueves")
    assert payload is not None
    assert payload.chat_id == GROUP and payload.chat_kind == "group"
    assert payload.chat_title == "Fútbol jueves"
    assert payload.text == "Mañana 7pm en la cancha"
    assert payload.sender is not None
    assert payload.sender.phone == "573009998877" and payload.sender.display_name == "Ana"
    assert payload.sender.is_self is False
    assert payload.media_kind == "none" and payload.reply_to_message_id is None
    assert external_id(_wm(msg)) == f"whatsapp:{GROUP}:3EB0ABCDEF"


def test_extended_text_with_reply_and_forward() -> None:
    msg = Message()
    msg.extendedTextMessage.text = "dale"
    msg.extendedTextMessage.contextInfo.stanzaID = "QUOTED1"
    msg.extendedTextMessage.contextInfo.isForwarded = True
    payload = build_payload(_wm(msg, from_me=True), chat_title="")
    assert payload is not None
    assert payload.text == "dale"
    assert payload.reply_to_message_id == "QUOTED1"
    assert payload.forwarded is True
    assert payload.sender is not None and payload.sender.is_self is True
    assert payload.chat_title is None


def test_image_with_caption_is_media_with_descriptor() -> None:
    msg = Message()
    msg.imageMessage.mimetype = "image/jpeg"
    msg.imageMessage.caption = "el recibo"
    msg.imageMessage.fileLength = 12345
    assert media_kind_of(msg) == "photo"
    assert text_of(msg) == "el recibo"
    assert media_descriptor(msg) == ("image/jpeg", "image.jpg", 12345)
    payload = build_payload(_wm(msg, chat=USER), chat_title="Ana")
    assert payload is not None
    assert payload.chat_kind == "user" and payload.media_kind == "photo"
    assert payload.media_caption == "el recibo"


def test_document_keeps_filename_and_voice_note_is_voice() -> None:
    doc = Message()
    doc.documentMessage.mimetype = "application/pdf"
    doc.documentMessage.fileName = "extracto.pdf"
    doc.documentMessage.fileLength = 10
    assert media_descriptor(doc) == ("application/pdf", "extracto.pdf", 10)
    assert media_kind_of(doc) == "document"
    voice = Message()
    voice.audioMessage.mimetype = "audio/ogg; codecs=opus"
    voice.audioMessage.PTT = True
    assert media_kind_of(voice) == "voice"
    assert media_descriptor(voice) == ("audio/ogg", "audio.ogg", 0)


def test_reactions_and_protocol_messages_are_not_content() -> None:
    reaction = Message()
    reaction.reactionMessage.text = "👍"
    assert not is_content_message(reaction)
    assert build_payload(_wm(reaction), chat_title="x") is None
    protocol = Message()
    protocol.protocolMessage.type = 0
    protocol.protocolMessage.key.ID = "X"
    assert not is_content_message(protocol)


def test_ephemeral_wrapper_is_unwrapped() -> None:
    msg = Message()
    msg.ephemeralMessage.message.conversation = "esto desaparece"
    assert text_of(msg) == "esto desaparece"
    assert build_payload(_wm(msg), chat_title="x") is not None


def test_history_message_in_group_uses_participant_and_timestamp() -> None:
    web = WebMessageInfo()
    web.key.remoteJID = GROUP
    web.key.fromMe = False
    web.key.ID = "HIST1"
    web.key.participant = "573001112233@s.whatsapp.net"
    web.messageTimestamp = 1756740600  # 2025-09-01 15:30 UTC
    web.pushName = "Ana"
    web.message.conversation = "hola"
    wm = from_history_message(web, GROUP)
    assert wm.sender == "573001112233" and wm.push_name == "Ana" and wm.message_id == "HIST1"
    assert wm.occurred_at == datetime(2025, 9, 1, 15, 30, tzinfo=UTC)
    assert not is_system_stub(web)


def test_history_message_in_dm_infers_sender_from_chat() -> None:
    web = WebMessageInfo()
    web.key.remoteJID = USER
    web.key.ID = "DM1"
    web.messageTimestamp = 1756740600
    web.message.conversation = "hey"
    assert from_history_message(web, USER).sender == "573001112233"
    mine = WebMessageInfo()
    mine.key.remoteJID = USER
    mine.key.fromMe = True
    mine.key.ID = "DM2"
    mine.message.conversation = "yo"
    assert from_history_message(mine, USER).sender == ""


def test_system_stub_is_detected() -> None:
    web = WebMessageInfo()
    web.key.remoteJID = GROUP
    web.key.ID = "STUB"
    web.messageStubType = 27  # alguien entró al grupo
    assert is_system_stub(web)


def test_ignored_chat_or_missing_id_yields_no_payload() -> None:
    msg = Message(conversation="estado")
    wm = WaMessage(
        chat_jid="status@broadcast",
        message_id="S1",
        from_me=False,
        sender="",
        push_name="",
        occurred_at=datetime.now(UTC),
        message=msg,
    )
    assert build_payload(wm, chat_title="") is None
