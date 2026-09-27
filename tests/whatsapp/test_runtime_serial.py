"""Los chunks del historial y los mensajes en vivo escriben en DB de a uno por cuenta."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

from memex.api.schemas import MediaItem
from memex.ingestors.whatsapp.config import WaAllowedChat
from memex.ingestors.whatsapp.parser import WaMessage
from memex.whatsapp import runtime as rt

GROUP = "120363000000000077@g.us"


class _Data:
    def __init__(self) -> None:
        self.conversations: list[Any] = []


@pytest.mark.asyncio
async def test_history_chunks_do_not_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dos chunks que llegan a la vez se procesan uno DESPUÉS del otro (el lock de la cuenta)."""
    runtime = rt.WhatsAppRuntime()
    linked = rt._Linked(account_id=1, user_id=1, client=object())
    active = 0
    max_active = 0

    def fake_chunk(_linked: Any, _data: Any) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        import time

        time.sleep(0.05)
        active -= 1
        return {"sync_type": "FULL", "conversations": 0, "ingested": 0, "buffered": 0, "skipped": 0}

    monkeypatch.setattr(runtime, "_ingest_history_chunk", fake_chunk)

    async def handle() -> None:
        async with linked.db_lock:
            await asyncio.to_thread(runtime._ingest_history_chunk, linked, _Data())

    await asyncio.gather(handle(), handle(), handle())
    assert max_active == 1


@pytest.mark.asyncio
async def test_drain_without_live_client_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = rt.WhatsAppRuntime()
    monkeypatch.setattr(runtime, "_drain_chat", lambda *a: (2, []))
    assert await runtime.drain(1, 99, ["a@g.us", "b@g.us"]) == 4
    assert runtime._tasks == set()


class _FakeClient:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.calls = 0

    async def download_any(self, message: Any) -> bytes:
        self.calls += 1
        if self.data is None:
            raise RuntimeError("404 media expired")
        return self.data


def _image_wm(mid: str) -> WaMessage:
    msg = Message()
    msg.imageMessage.mimetype = "image/jpeg"
    msg.imageMessage.fileLength = 3
    return WaMessage(
        chat_jid=GROUP,
        message_id=mid,
        from_me=False,
        sender="57300",
        push_name="Ana",
        occurred_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
        message=msg,
    )


def _linked_with(client: _FakeClient) -> rt._Linked:
    return rt._Linked(
        account_id=99,
        user_id=1,
        client=client,
        connected=True,
        allowed={GROUP: WaAllowedChat(chat_id=GROUP, name="Curso", mode="programa")},
    )


@pytest.mark.asyncio
async def test_drain_backfills_media_in_background(monkeypatch: pytest.MonkeyPatch) -> None:
    """El texto entra en el drain; el archivo de cada mensaje se baja después con el cliente vivo
    y se adjunta a su fila (fuera del lock de DB)."""
    runtime = rt.WhatsAppRuntime()
    client = _FakeClient(b"img")
    runtime._linked[99] = _linked_with(client)
    pending = [rt._MediaPending(inbox_id=7, wm=_image_wm("A"))]
    monkeypatch.setattr(runtime, "_drain_chat", lambda *a: (1, pending))
    attached: list[tuple[int, list[MediaItem]]] = []

    def fake_attach(_linked: Any, inbox_id: int, media: list[MediaItem]) -> int:
        attached.append((inbox_id, media))
        return len(media)

    monkeypatch.setattr(runtime, "_attach_media", fake_attach)

    assert await runtime.drain(1, 99, [GROUP]) == 1
    assert len(runtime._tasks) == 1
    await asyncio.gather(*runtime._tasks)
    assert client.calls == 1
    assert [i for i, _ in attached] == [7]
    (item,) = attached[0][1]
    assert (item.sha256, item.content_type, item.size) == (
        hashlib.sha256(b"img").hexdigest(),
        "image/jpeg",
        3,
    )
    assert runtime._tasks == set()


@pytest.mark.asyncio
async def test_backfill_counts_expired_media_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = rt.WhatsAppRuntime()
    linked = _linked_with(_FakeClient(None))
    calls: list[int] = []

    def fake_attach(*_a: Any) -> int:
        calls.append(1)
        return 1

    monkeypatch.setattr(runtime, "_attach_media", fake_attach)
    pending = [rt._MediaPending(inbox_id=1, wm=_image_wm("A")), rt._MediaPending(2, _image_wm("B"))]
    stats = await runtime._backfill_media(linked, pending)
    assert stats == {"requested": 2, "attached": 0, "failed": 2}
    assert calls == []


@pytest.mark.asyncio
async def test_backfill_offline_does_not_download() -> None:
    runtime = rt.WhatsAppRuntime()
    client = _FakeClient(b"img")
    linked = _linked_with(client)
    linked.connected = False
    stats = await runtime._backfill_media(linked, [rt._MediaPending(1, _image_wm("A"))])
    assert (stats["attached"], client.calls) == (0, 0)
