"""Allowlist de WhatsApp: formato de Telegram (chat_id = JID) + `since`."""

from __future__ import annotations

from datetime import date

from memex.ingestors.whatsapp.config import (
    WaAllowedChat,
    allowed_chats_to_config,
    parse_allowed_chats,
    session_db_path,
)


def test_parse_allowed_chats_tolerates_garbage_and_defaults_mode() -> None:
    cfg = {
        "allowed_chats": [
            {"chat_id": "1@g.us", "name": "Fútbol", "mode": "intimo", "since": "2026-01-15"},
            {"chat_id": "2@g.us", "mode": "raro"},
            {"chat_id": "3@g.us", "since": ""},
            {"sin_chat_id": True},
            "no-es-dict",
            {"chat_id": "4@g.us", "since": "no-es-fecha"},
        ]
    }
    allowed = parse_allowed_chats(cfg)
    assert set(allowed) == {"1@g.us", "2@g.us", "3@g.us"}
    assert allowed["1@g.us"].mode == "intimo" and allowed["1@g.us"].since == date(2026, 1, 15)
    assert allowed["2@g.us"].mode == "anuncios"
    assert allowed["3@g.us"].since is None
    assert parse_allowed_chats(None) == {}
    assert parse_allowed_chats({}) == {}


def test_since_gates_messages() -> None:
    chat = WaAllowedChat(chat_id="1@g.us", since=date(2026, 3, 1))
    assert not chat.admits(date(2026, 2, 28))
    assert chat.admits(date(2026, 3, 1))
    assert WaAllowedChat(chat_id="1@g.us").admits(date(2000, 1, 1))


def test_round_trip_to_config() -> None:
    chats = [
        WaAllowedChat(chat_id="1@g.us", name="A", mode="programa", since=date(2026, 1, 1)),
        WaAllowedChat(chat_id="2@g.us"),
    ]
    out = allowed_chats_to_config(chats)
    assert out == [
        {"chat_id": "1@g.us", "name": "A", "mode": "programa", "since": "2026-01-01"},
        {"chat_id": "2@g.us", "name": "", "mode": "anuncios", "since": None},
    ]
    assert parse_allowed_chats({"allowed_chats": out}) == {c.chat_id: c for c in chats}


def test_session_db_path_is_per_account(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MEMEX_WA_SESSION_PATH", "/tmp/wa")
    assert session_db_path(7).as_posix() == "/tmp/wa/account_7.db"
