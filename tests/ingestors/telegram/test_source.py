"""TelegramSource — cursor flow, streaming exclusion, idempotencia.

Mockea `TelegramClientWrapper` y `parse_telegram_message` para no necesitar
Telethon real. Verifica el contrato `Source[TelegramCursor]`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from memex.core.cursors import ChatCursor, TelegramCursor
from memex.core.source import HealthResult, SourceKind, SourceRecord
from memex.ingestors.telegram.config import AllowedChat, TelegramConfig
from memex.ingestors.telegram.source import TelegramSource, make_source


def _cfg(allowed_chats: list[AllowedChat] | None = None) -> TelegramConfig:
    return TelegramConfig(
        api_id=12345,
        api_hash="dead",
        phone="+34999",
        session_path=Path("/tmp/x"),
        session_name="test",
        allowed_chats=allowed_chats or [],
        batch_size=10,
    )


def _record(chat_id: int, message_id: int, topic_id: int | None = None) -> SourceRecord:
    return SourceRecord(
        external_id=f"telegram:{chat_id}:{message_id}",
        occurred_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        payload={
            "chat_id": chat_id,
            "chat_kind": "supergroup",
            "message_id": message_id,
            "topic_id": topic_id,
            "date": "2026-05-28T10:00:00+00:00",
            "text": "hi",
            "sender": None,
            "chat_title": None,
            "reply_to_message_id": None,
            "forwarded_from": None,
            "media_kind": "none",
            "media_caption": None,
        },
        dedupe_keys=[f"telegram:{chat_id}:{message_id}"],
    )


# ---- Contract attrs ---- #


def test_telegram_source_satisfies_contract() -> None:
    cfg = _cfg()
    src = TelegramSource(cfg)
    assert TelegramSource.type == "telegram"
    assert TelegramSource.kind is SourceKind.CHAT
    assert TelegramSource.payload_schema.__name__ == "TelegramPayload"
    assert TelegramSource.config_schema is TelegramConfig
    assert TelegramSource.checkpoint_schema is TelegramCursor
    assert src is not None


# ---- make_source factory ---- #


def test_make_source_validates_and_returns_telegram_source() -> None:
    env = {
        "MEMEX_TG_API_ID": "12345",
        "MEMEX_TG_API_HASH": "dead",
        "MEMEX_TG_PHONE": "+34999",
    }
    # make_source uses os.environ; we patch it via monkeypatch alternative.
    with patch.dict("os.environ", env, clear=False):
        src = make_source({"allowed_chats": [{"chat_id": -100, "topic_ids": [1]}]})
    assert isinstance(src, TelegramSource)
    assert len(src.cfg.allowed_chats) == 1


# ---- fetch: streaming exclusion ---- #


def test_fetch_skips_when_only_streaming_chats() -> None:
    """Chats con streaming=True quedan fuera del polling. Si no hay polling
    chats, fetch() retorna sin abrir conexión Telethon."""
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100, streaming=True)])
    src = TelegramSource(cfg)

    with patch("memex.ingestors.telegram.source.TelegramClientWrapper") as mock_wrapper:
        records = list(src.fetch(TelegramCursor()))

    assert records == []
    mock_wrapper.assert_not_called()  # client NUNCA debe construirse


def test_fetch_includes_polling_chats_excludes_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(
        allowed_chats=[
            AllowedChat(chat_id=-100),  # polling
            AllowedChat(chat_id=-200, streaming=True),  # excluded
            AllowedChat(chat_id=-300),  # polling
        ]
    )
    src = TelegramSource(cfg)

    chats_queried: list[int] = []

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            self.cfg = cfg

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            chats_queried.append(chat_id)
            return
            yield  # pragma: no cover

    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr("memex.ingestors.telegram._common.parse_telegram_message", lambda _m: None)

    list(src.fetch(TelegramCursor()))
    assert chats_queried == [-100, -300]  # -200 excluded


# ---- fetch: cursor flow ---- #


def test_fetch_passes_per_chat_min_id_from_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100), AllowedChat(chat_id=-200)])
    src = TelegramSource(cfg)
    cursor = TelegramCursor(
        chats={
            "-100": ChatCursor(last_message_id=42),
            "-200": ChatCursor(last_message_id=99),
        }
    )

    seen: list[tuple[int, int]] = []

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            seen.append((chat_id, min_id))
            return
            yield  # pragma: no cover

    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr("memex.ingestors.telegram._common.parse_telegram_message", lambda _m: None)

    list(src.fetch(cursor))
    assert seen == [(-100, 42), (-200, 99)]


def test_fetch_uses_min_id_zero_when_chat_not_in_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100)])
    src = TelegramSource(cfg)
    cursor = TelegramCursor()  # empty

    seen: list[tuple[int, int]] = []

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            seen.append((chat_id, min_id))
            return
            yield  # pragma: no cover

    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr("memex.ingestors.telegram._common.parse_telegram_message", lambda _m: None)

    list(src.fetch(cursor))
    assert seen == [(-100, 0)]


def test_fetch_yields_records_from_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100)])
    src = TelegramSource(cfg)

    expected = [_record(-100, 1), _record(-100, 2), _record(-100, 3)]

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            for fake_msg in [object(), object(), object()]:
                yield fake_msg

    parse_calls: list[Any] = []

    def _fake_parse(msg: Any) -> SourceRecord:
        i = len(parse_calls)
        parse_calls.append(msg)
        return expected[i]

    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr("memex.ingestors.telegram._common.parse_telegram_message", _fake_parse)

    records = list(src.fetch(TelegramCursor()))
    assert records == expected


def test_fetch_drops_records_when_parser_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parse_telegram_message returning None (e.g. for DMs) must be filtered."""
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100)])
    src = TelegramSource(cfg)

    kept = _record(-100, 1)

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            for _ in range(3):
                yield object()

    return_seq = iter([None, kept, None])
    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr(
        "memex.ingestors.telegram._common.parse_telegram_message",
        lambda _m: next(return_seq),
    )

    records = list(src.fetch(TelegramCursor()))
    assert records == [kept]


def test_fetch_topic_filter_drops_records_in_disallowed_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Records con topic_id que no esté en allowed_chat.topic_ids se filtran."""
    cfg = _cfg(allowed_chats=[AllowedChat(chat_id=-100, topic_ids=[5])])
    src = TelegramSource(cfg)

    in_topic = _record(-100, 1, topic_id=5)
    out_topic = _record(-100, 2, topic_id=99)
    no_topic = _record(-100, 3, topic_id=None)

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            for _ in range(3):
                yield object()

    return_seq = iter([in_topic, out_topic, no_topic])
    monkeypatch.setattr("memex.ingestors.telegram.source.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr(
        "memex.ingestors.telegram._common.parse_telegram_message",
        lambda _m: next(return_seq),
    )

    records = list(src.fetch(TelegramCursor()))
    assert records == [in_topic]  # only the in-topic one


# ---- advance_checkpoint ---- #


def test_advance_checkpoint_initializes_chat_entry() -> None:
    src = TelegramSource(_cfg())
    new_cp = src.advance_checkpoint(TelegramCursor(), _record(-100, 42))
    assert new_cp.chats == {"-100": ChatCursor(last_message_id=42)}


def test_advance_checkpoint_updates_existing_chat() -> None:
    src = TelegramSource(_cfg())
    existing = TelegramCursor(chats={"-100": ChatCursor(last_message_id=10)})
    new_cp = src.advance_checkpoint(existing, _record(-100, 99))
    assert new_cp.chats["-100"].last_message_id == 99


def test_advance_checkpoint_adds_new_chat_without_touching_others() -> None:
    src = TelegramSource(_cfg())
    existing = TelegramCursor(chats={"-100": ChatCursor(last_message_id=10)})
    new_cp = src.advance_checkpoint(existing, _record(-200, 5))
    assert new_cp.chats["-100"].last_message_id == 10
    assert new_cp.chats["-200"].last_message_id == 5


def test_advance_checkpoint_ignores_non_telegram_external_id() -> None:
    src = TelegramSource(_cfg())
    bad = SourceRecord(
        external_id="imap:server:1:2",
        occurred_at=datetime(2026, 5, 28, tzinfo=UTC),
        payload={},
        dedupe_keys=[],
    )
    existing = TelegramCursor(chats={"-100": ChatCursor(last_message_id=10)})
    assert src.advance_checkpoint(existing, bad) is existing


def test_advance_checkpoint_rejects_external_id_with_extra_colons() -> None:
    """external_id formato estricto: `telegram:<chat_id>:<message_id>` (3 partes).
    Cualquier extra colon en el id corrompería el parsing — rechazar fail-closed."""
    src = TelegramSource(_cfg())
    bad = SourceRecord(
        external_id="telegram:-100:42:extra",
        occurred_at=datetime(2026, 5, 28, tzinfo=UTC),
        payload={},
        dedupe_keys=[],
    )
    existing = TelegramCursor()
    assert src.advance_checkpoint(existing, bad) is existing


def test_advance_checkpoint_rejects_external_id_with_too_few_parts() -> None:
    src = TelegramSource(_cfg())
    bad = SourceRecord(
        external_id="telegram:42",
        occurred_at=datetime(2026, 5, 28, tzinfo=UTC),
        payload={},
        dedupe_keys=[],
    )
    existing = TelegramCursor()
    assert src.advance_checkpoint(existing, bad) is existing


def test_advance_checkpoint_ignores_malformed_id() -> None:
    src = TelegramSource(_cfg())
    bad = SourceRecord(
        external_id="telegram:not-an-int:42",
        occurred_at=datetime(2026, 5, 28, tzinfo=UTC),
        payload={},
        dedupe_keys=[],
    )
    existing = TelegramCursor()
    assert src.advance_checkpoint(existing, bad) is existing


def test_advance_checkpoint_ignores_zero_message_id() -> None:
    src = TelegramSource(_cfg())
    bad = SourceRecord(
        external_id="telegram:-100:0",
        occurred_at=datetime(2026, 5, 28, tzinfo=UTC),
        payload={},
        dedupe_keys=[],
    )
    existing = TelegramCursor()
    assert src.advance_checkpoint(existing, bad) is existing


# ---- health_check ---- #


@pytest.mark.asyncio
async def test_health_check_returns_healthy_when_client_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get_me(self) -> Any:
            class _Me:
                id = 12345

            return _Me()

    # health_check delega a _common.telegram_health_probe, que abre el wrapper.
    monkeypatch.setattr("memex.ingestors.telegram._common.TelegramClientWrapper", _FakeTC)
    result = await TelegramSource(_cfg()).health_check()
    assert isinstance(result, HealthResult)
    assert result.status == "healthy"
    assert "12345" in result.detail


@pytest.mark.asyncio
async def test_health_check_returns_unhealthy_when_client_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadTC:
        def __init__(self, cfg: TelegramConfig) -> None:
            pass

        async def __aenter__(self) -> _BadTC:
            raise ConnectionRefusedError("DC unreachable")

        async def __aexit__(self, *a: Any) -> None:
            pass

    monkeypatch.setattr("memex.ingestors.telegram._common.TelegramClientWrapper", _BadTC)
    result = await TelegramSource(_cfg()).health_check()
    assert result.status == "unhealthy"
    assert "ConnectionRefusedError" in result.detail
