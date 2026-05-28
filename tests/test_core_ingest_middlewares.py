"""PersistMiddleware — terminal middleware que escribe en inbox.

Verifica: satisface el Protocol IngestMiddleware, inserta vía insert_record,
loggea inserted vs dedupe_conflict, no llama next (es terminal), corre el
insert sync en threadpool.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from memex.core.inbox import InsertResult
from memex.core.ingest_middlewares import PersistMiddleware, noop_terminal
from memex.core.middleware import IngestContext, IngestMiddleware
from memex.core.source import SourceRecord


def _record(eid: str = "telegram:-100:42") -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        payload={"text": "hi"},
        dedupe_keys=[eid],
    )


def _ctx() -> IngestContext:
    return IngestContext(source_id=7, source_type="telegram", user_id=1)


class _FakeConn:
    """Conn fake — captura los insert_record calls. insert_record real recibe
    este objeto pero lo mockeamos vía monkeypatch del módulo, así que el conn
    solo necesita existir."""


def _conn_factory_capturing(opened: list[bool]) -> Any:
    @contextmanager
    def factory() -> Iterator[_FakeConn]:
        opened.append(True)
        yield _FakeConn()

    return factory


@pytest.mark.asyncio
async def test_persist_middleware_satisfies_protocol() -> None:
    mw = PersistMiddleware(connection_factory=_conn_factory_capturing([]))
    assert isinstance(mw, IngestMiddleware)


@pytest.mark.asyncio
async def test_persist_inserts_record(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[int, int, str]] = []

    def fake_insert(
        conn: Any, *, user_id: int, source_id: int, record: SourceRecord
    ) -> InsertResult:
        captured.append((user_id, source_id, record.external_id))
        return InsertResult(inserted=True, id=99)

    monkeypatch.setattr("memex.core.ingest_middlewares.insert_record", fake_insert)
    opened: list[bool] = []
    mw = PersistMiddleware(connection_factory=_conn_factory_capturing(opened))

    next_called: list[SourceRecord] = []

    async def _next(r: SourceRecord) -> None:
        next_called.append(r)

    await mw(_record(), _ctx(), _next)

    assert captured == [(1, 7, "telegram:-100:42")]
    assert opened == [True]  # opened a connection
    assert next_called == []  # terminal — does NOT call next


@pytest.mark.asyncio
async def test_persist_dedupe_conflict_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_insert(conn: Any, **kw: Any) -> InsertResult:
        return InsertResult(inserted=False, id=None, reason="duplicate")

    monkeypatch.setattr("memex.core.ingest_middlewares.insert_record", fake_insert)
    mw = PersistMiddleware(connection_factory=_conn_factory_capturing([]))

    async def _next(r: SourceRecord) -> None:
        raise AssertionError("terminal must not call next")

    # Should complete without raising even though insert was a duplicate.
    await mw(_record(), _ctx(), _next)


@pytest.mark.asyncio
async def test_persist_propagates_insert_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad source_id (ValueError from insert_record) propagates so the
    runner's supervisor can decide to reconnect / dead-letter."""

    def fake_insert(conn: Any, **kw: Any) -> InsertResult:
        raise ValueError("source_id 7 does not belong to user 1")

    monkeypatch.setattr("memex.core.ingest_middlewares.insert_record", fake_insert)
    mw = PersistMiddleware(connection_factory=_conn_factory_capturing([]))

    async def _next(r: SourceRecord) -> None:
        pass

    with pytest.raises(ValueError, match="does not belong"):
        await mw(_record(), _ctx(), _next)


@pytest.mark.asyncio
async def test_noop_terminal_does_nothing() -> None:
    await noop_terminal(_record())  # must not raise
