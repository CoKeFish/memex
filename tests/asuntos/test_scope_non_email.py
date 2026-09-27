"""Alcance del asignador: los correos entran con veredicto `relevant` del gate; lo que no es correo
(chats, notas) no pasa por el gate y entra directo (antes quedaba fuera para siempre)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Connection, text

from memex.modules.asuntos.assign import _relevant
from memex.modules.asuntos.store import unseen_relevant_inbox_ids


def _source(conn: Connection, type_: str) -> int:
    return int(
        conn.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": f"{type_}-scope", "t": type_},
        ).scalar_one()
    )


def _inbox(conn: Connection, sid: int, eid: str, payload: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :e, :o, CAST(:p AS JSONB)) RETURNING id"
            ),
            {"s": sid, "e": eid, "o": datetime(2026, 9, 1, tzinfo=UTC), "p": payload},
        ).scalar_one()
    )


def test_non_email_messages_enter_without_gate_verdict(conn: Connection) -> None:
    imap, notion = _source(conn, "imap"), _source(conn, "notion")
    mail_ok = _inbox(conn, imap, "m1", '{"subject": "a", "body_text": "b"}')
    mail_nogate = _inbox(conn, imap, "m2", '{"subject": "c", "body_text": "d"}')
    mail_not = _inbox(conn, imap, "m3", '{"subject": "e", "body_text": "f"}')
    note = _inbox(conn, notion, "notion:p1", '{"page_id": "p1", "title": "t", "body_text": "x"}')
    conn.execute(
        text(
            "INSERT INTO relevance_verdicts (user_id, inbox_id, verdict, method) "
            "VALUES (1, :a, 'relevant', 'rule'), (1, :b, 'not_relevant', 'rule')"
        ),
        {"a": mail_ok, "b": mail_not},
    )
    got = _relevant(conn, 1, [mail_ok, mail_nogate, mail_not, note])
    assert sorted(got) == sorted([mail_ok, note])
    pending = unseen_relevant_inbox_ids(conn, 1, limit=50)
    assert sorted(pending) == sorted([mail_ok, note])
