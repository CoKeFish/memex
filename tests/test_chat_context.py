"""Naturaleza por chat (allowlist de la fuente) y lo que decide en asuntos e identidades."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from memex.modules.asuntos.adhesion import load_messages
from memex.modules.asuntos.assign import SYSTEM_PROMPT, render_batch
from memex.modules.chat_context import chat_modes_for_source, mode_label
from memex.modules.identidades.senders import ensure_chat_sender_identities

CHAT = -1001000000001
OTHER = -1001000000002


def _source(conn: Connection, allowed: list[dict[str, Any]]) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config) "
                "VALUES (1, 'tg-modes', 'telegram', CAST(:c AS JSONB)) RETURNING id"
            ),
            {"c": json.dumps({"allowed_chats": allowed})},
        ).scalar_one()
    )


def _msg(
    conn: Connection,
    sid: int,
    mid: int,
    *,
    chat_id: int = CHAT,
    user_id: int = 9001,
    name: str = "Ana",
    text_: str = "hola",
    is_self: bool = False,
    reply_to: int | None = None,
) -> int:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "chat_kind": "supergroup",
        "chat_title": "Comunidad X",
        "message_id": mid,
        "sender": {"user_id": user_id, "display_name": name, "is_bot": False, "is_self": is_self},
        "text": text_,
        "date": "2026-09-01T10:00:00Z",
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    return int(
        conn.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, :o, CAST(:p AS JSONB)) RETURNING id"
            ),
            {
                "s": sid,
                "e": f"{chat_id}:{mid}",
                "o": datetime(2026, 9, 1, 10, mid % 60, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar_one()
    )


def test_modes_come_from_the_allowlist_with_default(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "programa"}, {"chat_id": OTHER}])
    modes = chat_modes_for_source(conn, sid)
    assert modes.by_chat == {str(CHAT): "programa", str(OTHER): "anuncios"}
    assert modes.of({"chat_id": str(CHAT)}) == "programa"
    assert modes.of({"chat_id": 123}) == "anuncios"
    assert modes.of({}) == "anuncios"
    assert "comunidad" in mode_label("anuncios") and "participa" in mode_label("programa")


def test_asuntos_sees_chat_nature_and_owner_messages(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "programa"}])
    theirs = _msg(conn, sid, 1, text_="Ya entregué mi tarea")
    mine = _msg(conn, sid, 2, user_id=1, name="Yo", text_="Entregué la mía", is_self=True)
    views = load_messages(conn, 1, [theirs, mine], body_chars=200)
    assert [v.chat_mode for v in views] == ["programa", "programa"]
    assert views[0].chat_title == "Comunidad X" and views[0].is_self is False
    assert views[1].is_self is True
    rendered = render_batch(views)
    assert "grupo «Comunidad X» · programa en el que el usuario participa" in rendered
    assert rendered.count("LO ESCRIBIÓ EL USUARIO") == 1
    assert "CHATS DE GRUPO" in SYSTEM_PROMPT and "LECTOR" in SYSTEM_PROMPT


def test_chat_senders_earn_identity_by_nature(conn: Connection) -> None:
    sid = _source(
        conn,
        [{"chat_id": CHAT, "mode": "programa"}, {"chat_id": OTHER, "mode": "intimo"}],
    )
    ids: list[int] = []
    # Programa: uno que habla una vez NO entra; el organizador recurrente (5 msgs) SÍ.
    ids.append(_msg(conn, sid, 10, user_id=111, name="Visitante", text_="hola a todos"))
    for i in range(5):
        ids.append(_msg(conn, sid, 20 + i, user_id=222, name="Organizadora", text_=f"aviso {i}"))
    # Quien le responde al usuario SÍ (aunque hable una vez).
    mine = _msg(conn, sid, 30, user_id=1, name="Yo", text_="¿alguien sabe?", is_self=True)
    ids.append(mine)
    ids.append(_msg(conn, sid, 31, user_id=333, name="Amable", text_="sí, mirá…", reply_to=30))
    # Grupo íntimo: todos entran.
    ids.append(_msg(conn, sid, 40, chat_id=OTHER, user_id=444, name="Primo", text_="qué más"))
    created = ensure_chat_sender_identities(conn, 1, ids)
    names = set(
        conn.execute(
            text("SELECT display_name FROM mod_identidades WHERE user_id = 1 AND kind = 'persona'")
        ).scalars()
    )
    assert "Visitante" not in names
    assert {"Organizadora", "Amable", "Primo", "Yo"} <= names
    assert created == 4


def test_community_chat_recurrent_sender_gets_no_card(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "anuncios"}])
    ids = [_msg(conn, sid, 50 + i, user_id=555, name="Activo", text_=f"msg {i}") for i in range(8)]
    assert ensure_chat_sender_identities(conn, 1, ids) == 0
