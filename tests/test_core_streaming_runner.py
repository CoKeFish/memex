"""Tests para `memex.core.streaming_runner.StreamingRunner`.

Verifica el ciclo: catchup → listen → reconnect en error → dead-letter después
de N intentos → cleanup en stop().

Usa una FakeStreamingSource in-memory para no depender de ninguna fuente real.
"""

from __future__ import annotations

import asyncio
from builtins import type as _type
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from memex.core.source import SourceRecord
from memex.core.streaming import StreamHandler
from memex.core.streaming_runner import (
    RegisteredStreamingSource,
    StreamingRunner,
)


class _FakeCursor(BaseModel):
    """Cursor in-memory para la fake source."""

    seen: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


def _rec(eid: str) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 27, 0, 0, tzinfo=UTC),
        payload={"eid": eid},
        dedupe_keys=[],
    )


class _FakeStreamingSource:
    """Fake source que emite catchup pre-seedeado y luego eventos via inject().

    No usa Telethon ni I/O — solo asyncio.Event para sincronizar el test.
    """

    type: ClassVar[str] = "fake"
    checkpoint_schema: ClassVar[_type[BaseModel]] = _FakeCursor

    def __init__(
        self,
        catchup_records: list[SourceRecord] | None = None,
        listen_fail_times: int = 0,
    ) -> None:
        self._catchup = catchup_records or []
        self._inject_queue: asyncio.Queue[SourceRecord | None] = asyncio.Queue()
        self._listen_fail_remaining = listen_fail_times
        self._listen_started = asyncio.Event()
        self.disconnected = False
        self.listen_invocations = 0

    def advance_checkpoint(self, checkpoint: _FakeCursor, last: SourceRecord) -> _FakeCursor:
        return _FakeCursor(seen=[*checkpoint.seen, last.external_id])

    async def catchup(self, checkpoint: _FakeCursor) -> AsyncIterator[SourceRecord]:
        for r in self._catchup:
            yield r

    async def listen(self, on_record: StreamHandler) -> None:
        self.listen_invocations += 1
        self._listen_started.set()
        if self._listen_fail_remaining > 0:
            self._listen_fail_remaining -= 1
            raise RuntimeError("simulated listen failure")
        while True:
            record = await self._inject_queue.get()
            if record is None:
                return
            await on_record(record)

    async def disconnect(self) -> None:
        self.disconnected = True
        await self._inject_queue.put(None)

    async def inject(self, record: SourceRecord) -> None:
        await self._inject_queue.put(record)

    async def wait_until_listening(self) -> None:
        await self._listen_started.wait()
        self._listen_started.clear()


class _InMemoryCheckpointStore:
    """Store dict-based para que el runner no necesite DB en tests."""

    def __init__(self) -> None:
        self._store: dict[int, dict[str, Any]] = {}
        self.save_calls: list[tuple[int, dict[str, Any]]] = []

    def load(self, source_id: int) -> dict[str, Any] | None:
        return self._store.get(source_id)

    def save(self, source_id: int, cursor: dict[str, Any]) -> None:
        self._store[source_id] = cursor
        self.save_calls.append((source_id, dict(cursor)))


@pytest.mark.asyncio
async def test_catchup_processes_pending_records_and_advances_cursor() -> None:
    seen: list[str] = []

    async def handler(rec: SourceRecord) -> None:
        seen.append(rec.external_id)

    src = _FakeStreamingSource(catchup_records=[_rec("a"), _rec("b")])
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner(
        [RegisteredStreamingSource(source=src, source_id=1, handler=handler)],
        load_checkpoint=store.load,
        save_checkpoint=store.save,
    )

    await runner.start()
    await src.wait_until_listening()
    await runner.stop()

    assert seen == ["a", "b"]
    assert store._store[1] == {"seen": ["a", "b"]}


@pytest.mark.asyncio
async def test_listen_events_are_processed_and_advance_cursor() -> None:
    seen: list[str] = []

    async def handler(rec: SourceRecord) -> None:
        seen.append(rec.external_id)

    src = _FakeStreamingSource()
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner(
        [RegisteredStreamingSource(source=src, source_id=1, handler=handler)],
        load_checkpoint=store.load,
        save_checkpoint=store.save,
    )

    await runner.start()
    await src.wait_until_listening()
    await src.inject(_rec("e1"))
    await src.inject(_rec("e2"))
    await asyncio.sleep(0.05)  # let handler drain
    await runner.stop()

    assert seen == ["e1", "e2"]
    assert store._store[1] == {"seen": ["e1", "e2"]}


@pytest.mark.asyncio
async def test_supervisor_reconnects_on_listen_failure() -> None:
    seen: list[str] = []

    async def handler(rec: SourceRecord) -> None:
        seen.append(rec.external_id)

    # Fail listen 2 times, then succeed
    src = _FakeStreamingSource(listen_fail_times=2)
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner(
        [RegisteredStreamingSource(source=src, source_id=1, handler=handler)],
        load_checkpoint=store.load,
        save_checkpoint=store.save,
        initial_backoff_s=0.01,
        max_backoff_s=0.05,
    )

    await runner.start()
    # Wait for 3rd listen invocation to start
    for _ in range(20):
        if src.listen_invocations >= 3:
            break
        await asyncio.sleep(0.02)
    await src.inject(_rec("after-reconnect"))
    await asyncio.sleep(0.05)
    await runner.stop()

    assert src.listen_invocations >= 3
    assert "after-reconnect" in seen


@pytest.mark.asyncio
async def test_supervisor_dead_letters_after_max_retries() -> None:
    async def handler(rec: SourceRecord) -> None:
        pass

    # Fail 100 times — max_retries=2 → dead-letter
    src = _FakeStreamingSource(listen_fail_times=100)
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner(
        [RegisteredStreamingSource(source=src, source_id=1, handler=handler)],
        load_checkpoint=store.load,
        save_checkpoint=store.save,
        initial_backoff_s=0.01,
        max_backoff_s=0.02,
        max_retries=2,
    )

    await runner.start()
    # Wait until the supervisor gives up
    for _ in range(50):
        await asyncio.sleep(0.02)
        if all(t.done() for t in runner._tasks):
            break
    assert all(t.done() for t in runner._tasks)
    # Listen was invoked max_retries+1 times before dead-letter
    assert src.listen_invocations == 3
    await runner.stop()


@pytest.mark.asyncio
async def test_empty_sources_list_starts_without_error() -> None:
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner([], load_checkpoint=store.load, save_checkpoint=store.save)
    await runner.start()
    await runner.stop()


@pytest.mark.asyncio
async def test_stop_calls_disconnect_on_each_source() -> None:
    async def handler(rec: SourceRecord) -> None:
        pass

    src = _FakeStreamingSource()
    store = _InMemoryCheckpointStore()
    runner = StreamingRunner(
        [RegisteredStreamingSource(source=src, source_id=1, handler=handler)],
        load_checkpoint=store.load,
        save_checkpoint=store.save,
    )
    await runner.start()
    await src.wait_until_listening()
    await runner.stop()
    assert src.disconnected is True
