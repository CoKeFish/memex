"""Nombres y LIDs de WhatsApp: `SenderAlt` en vivo, tabla de pushnames del historial, LID → número
con caché, y renombrado de fichas placeholder cuando el nombre aparece."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.proto.waWeb.WAWebProtobufsWeb_pb2 import WebMessageInfo
from sqlalchemy import text

from memex.db import connection
from memex.ingestors.whatsapp.parser import WaMessage, from_history_message, from_live_event
from memex.modules.identidades.senders import rename_placeholder_senders, weave_chat_structure
from memex.whatsapp import runtime as rt

GROUP = "120363000000000042@g.us"


def _jid(user: str, server: str) -> Any:
    return SimpleNamespace(User=user, Server=server)


def _event(*, sender: Any, alt: Any = None, name: str = "") -> Any:
    src = SimpleNamespace(
        Chat=_jid("120363000000000042", "g.us"),
        Sender=sender,
        SenderAlt=alt,
        IsFromMe=False,
        IsGroup=True,
    )
    info = SimpleNamespace(ID="M1", MessageSource=src, Pushname=name, Timestamp=1756740600)
    return SimpleNamespace(Info=info, Message=Message(conversation="hola"))


def _wm(sender: str, push_name: str, *, lid: bool = False) -> WaMessage:
    return WaMessage(
        chat_jid=GROUP,
        message_id="X",
        from_me=False,
        sender=sender,
        push_name=push_name,
        occurred_at=datetime.now(UTC),
        message=None,
        sender_is_lid=lid,
    )


def test_live_event_prefers_phone_over_lid() -> None:
    wm = from_live_event(
        _event(sender=_jid("18198221035", "lid"), alt=_jid("573001112233", "s.whatsapp.net"))
    )
    assert wm.sender == "573001112233" and wm.sender_is_lid is False
    wm2 = from_live_event(_event(sender=_jid("18198221035", "lid"), alt=_jid("", "")))
    assert wm2.sender == "18198221035" and wm2.sender_is_lid is True
    wm3 = from_live_event(_event(sender=_jid("573001112233", "s.whatsapp.net")))
    assert wm3.sender == "573001112233" and wm3.sender_is_lid is False


def test_history_message_flags_lid_participant() -> None:
    web = WebMessageInfo()
    web.key.remoteJID = GROUP
    web.key.ID = "H1"
    web.key.participant = "18198221035@lid"
    web.message.conversation = "x"
    wm = from_history_message(web, GROUP)
    assert wm.sender == "18198221035" and wm.sender_is_lid is True


def test_pushnames_table_names_history_messages() -> None:
    linked = rt._Linked(account_id=1, user_id=1, client=object())
    data = SimpleNamespace(
        pushnames=[
            SimpleNamespace(ID="573001112233@s.whatsapp.net", pushname="Ana"),
            SimpleNamespace(ID="18198221035@lid", pushname="Luis"),
            SimpleNamespace(ID="", pushname="nadie"),
        ]
    )
    assert rt._absorb_pushnames(linked, data) == 2
    linked.lid_to_phone["18198221035"] = "573009998877"
    by_phone = rt._named(linked, _wm("573001112233", ""))
    assert by_phone.push_name == "Ana"
    by_lid = rt._named(linked, _wm("18198221035", "", lid=True))
    assert by_lid.sender == "573009998877" and by_lid.push_name == "Luis"
    assert by_lid.sender_is_lid is False
    keeps = rt._named(linked, _wm("5", "Ya tiene"))
    assert keeps.push_name == "Ya tiene"


@pytest.mark.asyncio
async def test_resolve_lid_uses_session_and_caches() -> None:
    calls: list[str] = []

    class _Client:
        async def get_pn_from_lid(self, jid: Any) -> Any:
            calls.append(jid.User)
            return SimpleNamespace(User="573001112233")

    runtime = rt.WhatsAppRuntime()
    linked = rt._Linked(account_id=1, user_id=1, client=_Client())
    assert await runtime._resolve_lid(linked, "181") == "573001112233"
    assert await runtime._resolve_lid(linked, "181") == "573001112233"
    assert calls == ["181"]  # cacheado

    class _Broken:
        async def get_pn_from_lid(self, jid: Any) -> Any:
            raise RuntimeError("sin mapeo")

    linked2 = rt._Linked(account_id=1, user_id=1, client=_Broken())
    assert await runtime._resolve_lid(linked2, "999") == "999"  # se queda con el LID


def _source() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type, config) VALUES (1, :n, 'whatsapp', "
                    "CAST(:cfg AS JSONB)) RETURNING id"
                ),
                {
                    "n": f"wa-{uuid4().hex[:6]}",
                    "cfg": json.dumps({"allowed_chats": [{"chat_id": GROUP, "mode": "intimo"}]}),
                },
            ).scalar_one()
        )


def _inbox(source_id: int, phone: str, name: str | None) -> int:
    payload = {
        "chat_id": GROUP,
        "chat_kind": "group",
        "chat_title": "G",
        "message_id": uuid4().hex[:10],
        "sender": {"phone": phone, "display_name": name, "is_self": False},
        "date": "2026-09-01T12:00:00Z",
        "text": "hola",
    }
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": source_id, "e": uuid4().hex, "p": json.dumps(payload)},
            ).scalar_one()
        )


def _name_of(phone: str) -> str:
    with connection() as c:
        return str(
            c.execute(
                text(
                    "SELECT x.display_name FROM mod_identidades x "
                    "JOIN mod_identidades_identifiers f ON f.identity_id = x.id "
                    "WHERE f.platform = 'whatsapp' AND f.kind = 'platform_id' AND f.value_norm = :p"
                ),
                {"p": phone},
            ).scalar_one()
        )


def test_placeholder_sender_gets_renamed_when_a_name_arrives() -> None:
    src = _source()
    phone = "57" + uuid4().hex[:9].translate(str.maketrans("abcdef", "123456"))
    m1 = _inbox(src, phone, None)  # historial: sin nombre
    with connection() as c:
        weave_chat_structure(c, 1, [m1])
    assert _name_of(phone) == f"whatsapp {phone}"
    m2 = _inbox(src, phone, "Ana Pérez")  # en vivo: con nombre
    with connection() as c:
        renamed = rename_placeholder_senders(c, 1, [m2])
    assert renamed == 1
    assert _name_of(phone) == "Ana Pérez"
    with connection() as c:
        assert rename_placeholder_senders(c, 1, [m2]) == 0  # idempotente
