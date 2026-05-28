"""TelegramStreamingSource — catchup, listen, disconnect, contract, errores.

Mockea `TelegramClientWrapper` y `parse_telegram_message` (en el módulo
`streaming` y/o `_common` según el path) para no necesitar Telethon real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from memex.core.cursors import ChatCursor, TelegramCursor
from memex.core.source import HealthResult, SourceKind, SourceRecord
from memex.core.streaming import StreamingSource
from memex.ingestors.telegram.config import AllowedChat, TelegramConfig
from memex.ingestors.telegram.streaming import (
    TelegramStreamingSource,
    make_streaming_source,
)

_STREAMING_MOD = "memex.ingestors.telegram.streaming"
_COMMON_MOD = "memex.ingestors.telegram._common"


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
        payload={"chat_id": chat_id, "message_id": message_id, "topic_id": topic_id, "text": "hi"},
        dedupe_keys=[f"telegram:{chat_id}:{message_id}"],
    )


class _FakeEvent:
    """Evento Telethon fake — get_chat/get_sender ignorados (parse mockeado)."""

    def __init__(self, message: Any = None) -> None:
        self.message = message if message is not None else object()

    async def get_chat(self) -> Any:
        return object()

    async def get_sender(self) -> Any:
        return object()


EventCb = Callable[[Any], Awaitable[None]]


class _FakeStreamingClient:
    """Fake del TelegramClientWrapper para tests de listen.

    `run_until_disconnected` bloquea hasta `disconnect()`. El test inyecta
    eventos llamando el handler registrado en `add_new_message_handler`.
    """

    instances: ClassVar[list[_FakeStreamingClient]] = []

    def __init__(self, cfg: TelegramConfig, *, sequential_updates: bool = False) -> None:
        self.cfg = cfg
        self.sequential_updates = sequential_updates
        self.handler: EventCb | None = None
        self.chat_ids: list[int] = []
        self.disconnected = False
        self._stop = asyncio.Event()
        _FakeStreamingClient.instances.append(self)

    async def __aenter__(self) -> _FakeStreamingClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    def add_new_message_handler(self, callback: EventCb, chat_ids: list[int]) -> None:
        self.handler = callback
        self.chat_ids = chat_ids

    async def run_until_disconnected(self) -> None:
        await self._stop.wait()

    async def disconnect(self) -> None:
        self.disconnected = True
        self._stop.set()


@pytest.fixture(autouse=True)
def _clear_instances() -> None:
    _FakeStreamingClient.instances.clear()


# ---- contract ---- #


def test_streaming_source_satisfies_contract() -> None:
    src = TelegramStreamingSource(_cfg())
    assert isinstance(src, StreamingSource)
    assert TelegramStreamingSource.type == "telegram"
    assert TelegramStreamingSource.kind is SourceKind.CHAT
    assert TelegramStreamingSource.payload_schema.__name__ == "TelegramPayload"
    assert TelegramStreamingSource.config_schema is TelegramConfig
    assert TelegramStreamingSource.checkpoint_schema is TelegramCursor


def test_make_streaming_source_factory() -> None:
    env = {"MEMEX_TG_API_ID": "1", "MEMEX_TG_API_HASH": "h", "MEMEX_TG_PHONE": "+34"}
    from unittest.mock import patch

    with patch.dict("os.environ", env, clear=False):
        src = make_streaming_source({"allowed_chats": [{"chat_id": -100, "streaming": True}]})
    assert isinstance(src, TelegramStreamingSource)


def test_advance_checkpoint_delegates_to_common() -> None:
    src = TelegramStreamingSource(_cfg())
    new_cp = src.advance_checkpoint(TelegramCursor(), _record(-100, 42))
    assert new_cp.chats == {"-100": ChatCursor(last_message_id=42)}


# ---- catchup ---- #


@pytest.mark.asyncio
async def test_catchup_skips_when_no_streaming_chats() -> None:
    # only polling chats → catchup yields nothing, opens no client
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=False)]))
    records = [r async for r in src.catchup(TelegramCursor())]
    assert records == []


@pytest.mark.asyncio
async def test_catchup_queries_only_streaming_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(
        [
            AllowedChat(chat_id=-100, streaming=True),
            AllowedChat(chat_id=-200, streaming=False),  # polling — excluded from catchup
            AllowedChat(chat_id=-300, streaming=True),
        ]
    )
    src = TelegramStreamingSource(cfg)
    queried: list[tuple[int, int]] = []

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig, *, sequential_updates: bool = False) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            queried.append((chat_id, min_id))
            return
            yield  # pragma: no cover

    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr(f"{_COMMON_MOD}.parse_telegram_message", lambda _m: None)

    cursor = TelegramCursor(chats={"-100": ChatCursor(last_message_id=7)})
    _ = [r async for r in src.catchup(cursor)]
    assert queried == [(-100, 7), (-300, 0)]  # -200 (polling) excluded


@pytest.mark.asyncio
async def test_catchup_yields_parsed_records(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg([AllowedChat(chat_id=-100, streaming=True)])
    src = TelegramStreamingSource(cfg)
    expected = [_record(-100, 1), _record(-100, 2)]

    class _FakeTC:
        def __init__(self, cfg: TelegramConfig, *, sequential_updates: bool = False) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_chat_messages(
            self, chat_id: int, *, min_id: int, batch_size: int
        ) -> AsyncIterator[Any]:
            for _ in range(2):
                yield object()

    seq = iter(expected)
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeTC)
    monkeypatch.setattr(f"{_COMMON_MOD}.parse_telegram_message", lambda _m: next(seq))

    records = [r async for r in src.catchup(TelegramCursor())]
    assert records == expected


# ---- listen ---- #


@pytest.mark.asyncio
async def test_listen_skips_when_no_streaming_chats() -> None:
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=False)]))
    received: list[SourceRecord] = []

    async def on_record(r: SourceRecord) -> None:
        received.append(r)

    await src.listen(on_record)  # returns immediately
    assert received == []


async def _start_listen(
    src: TelegramStreamingSource,
    on_record: EventCb,
) -> asyncio.Task[None]:
    task = asyncio.create_task(src.listen(on_record))
    # wait until the fake client registered its handler
    for _ in range(100):
        await asyncio.sleep(0.005)
        if _FakeStreamingClient.instances and _FakeStreamingClient.instances[-1].handler:
            break
    return task


@pytest.mark.asyncio
async def test_listen_delivers_event_to_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeStreamingClient)
    monkeypatch.setattr(
        f"{_STREAMING_MOD}.parse_telegram_message",
        lambda msg, *, chat, sender: _record(-100, 5),
    )
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=True)]))
    received: list[SourceRecord] = []

    async def on_record(r: SourceRecord) -> None:
        received.append(r)

    task = await _start_listen(src, on_record)
    fake = _FakeStreamingClient.instances[-1]
    assert fake.handler is not None
    assert fake.chat_ids == [-100]
    assert fake.sequential_updates is True  # ordered cursor advancement

    await fake.handler(_FakeEvent())
    await asyncio.sleep(0.01)
    await src.disconnect()
    await task

    assert [r.external_id for r in received] == ["telegram:-100:5"]


@pytest.mark.asyncio
async def test_listen_topic_filter_drops_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeStreamingClient)
    # record con topic_id=99, pero el chat solo permite topic 5
    monkeypatch.setattr(
        f"{_STREAMING_MOD}.parse_telegram_message",
        lambda msg, *, chat, sender: _record(-100, 5, topic_id=99),
    )
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, topic_ids=[5], streaming=True)]))
    received: list[SourceRecord] = []

    async def on_record(r: SourceRecord) -> None:
        received.append(r)

    task = await _start_listen(src, on_record)
    fake = _FakeStreamingClient.instances[-1]
    assert fake.handler is not None
    await fake.handler(_FakeEvent())
    await asyncio.sleep(0.01)
    await src.disconnect()
    await task

    assert received == []  # topic-filtered out


@pytest.mark.asyncio
async def test_listen_drops_event_for_unknown_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeStreamingClient)
    # record para un chat (-999) que no está en la allowlist streaming
    monkeypatch.setattr(
        f"{_STREAMING_MOD}.parse_telegram_message",
        lambda msg, *, chat, sender: _record(-999, 5),
    )
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=True)]))
    received: list[SourceRecord] = []

    async def on_record(r: SourceRecord) -> None:
        received.append(r)

    task = await _start_listen(src, on_record)
    fake = _FakeStreamingClient.instances[-1]
    assert fake.handler is not None
    await fake.handler(_FakeEvent())
    await asyncio.sleep(0.01)
    await src.disconnect()
    await task

    assert received == []


@pytest.mark.asyncio
async def test_listen_drops_when_parser_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeStreamingClient)
    monkeypatch.setattr(
        f"{_STREAMING_MOD}.parse_telegram_message",
        lambda msg, *, chat, sender: None,
    )
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=True)]))
    received: list[SourceRecord] = []

    async def on_record(r: SourceRecord) -> None:
        received.append(r)

    task = await _start_listen(src, on_record)
    fake = _FakeStreamingClient.instances[-1]
    assert fake.handler is not None
    await fake.handler(_FakeEvent())
    await asyncio.sleep(0.01)
    await src.disconnect()
    await task

    assert received == []


@pytest.mark.asyncio
async def test_listen_on_record_error_disconnects_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si on_record (persist) lanza, el handler desconecta y listen re-lanza
    para que el supervisor reconecte."""
    monkeypatch.setattr(f"{_STREAMING_MOD}.TelegramClientWrapper", _FakeStreamingClient)
    monkeypatch.setattr(
        f"{_STREAMING_MOD}.parse_telegram_message",
        lambda msg, *, chat, sender: _record(-100, 5),
    )
    src = TelegramStreamingSource(_cfg([AllowedChat(chat_id=-100, streaming=True)]))

    async def on_record(r: SourceRecord) -> None:
        raise RuntimeError("persist boom")

    task = await _start_listen(src, on_record)
    fake = _FakeStreamingClient.instances[-1]
    assert fake.handler is not None
    await fake.handler(_FakeEvent())

    with pytest.raises(RuntimeError, match="persist boom"):
        await task
    assert fake.disconnected is True


@pytest.mark.asyncio
async def test_disconnect_idempotent_when_not_listening() -> None:
    src = TelegramStreamingSource(_cfg())
    await src.disconnect()  # _wrapper is None — must not raise


# ---- health ---- #


@pytest.mark.asyncio
async def test_health_check_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTC:
        def __init__(self, cfg: TelegramConfig, *, sequential_updates: bool = False) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get_me(self) -> Any:
            class _Me:
                id = 777

            return _Me()

    monkeypatch.setattr(f"{_COMMON_MOD}.TelegramClientWrapper", _FakeTC)
    result = await TelegramStreamingSource(_cfg()).health_check()
    assert isinstance(result, HealthResult)
    assert result.status == "healthy"
    assert "777" in result.detail
