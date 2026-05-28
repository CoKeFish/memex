"""Tests para `memex.core.middleware.build_handler` y la chain.

Verifica orden, short-circuit (drop), propagación del contexto, y que el
terminal se invoca correctamente.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memex.core.middleware import (
    IngestContext,
    IngestMiddleware,
    Next,
    build_handler,
)
from memex.core.source import SourceRecord


def _rec(eid: str) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 27, 0, 0, tzinfo=UTC),
        payload={"eid": eid},
        dedupe_keys=[],
    )


def _ctx() -> IngestContext:
    return IngestContext(source_id=1, source_type="demo", user_id=42)


class _Tag(IngestMiddleware):
    """Middleware que registra su nombre y delega al next."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None:
        self.calls.append(f"in:{self.name}")
        await next(record)
        self.calls.append(f"out:{self.name}")


class _Drop(IngestMiddleware):
    """Middleware que hace drop (no llama next)."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None:
        self.calls.append(f"drop:{self.name}")
        # Intencionalmente NO llama next


class _CaptureCtx(IngestMiddleware):
    """Middleware que captura el contexto que recibió."""

    def __init__(self) -> None:
        self.seen_ctx: IngestContext | None = None

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None:
        self.seen_ctx = ctx
        await next(record)


@pytest.mark.asyncio
async def test_chain_invokes_middlewares_in_order() -> None:
    calls: list[str] = []

    async def terminal(rec: SourceRecord) -> None:
        calls.append(f"terminal:{rec.external_id}")

    handler = build_handler(
        [_Tag("a", calls), _Tag("b", calls), _Tag("c", calls)],
        terminal,
        _ctx(),
    )
    await handler(_rec("r1"))

    # a -> b -> c -> terminal -> c -> b -> a
    assert calls == [
        "in:a",
        "in:b",
        "in:c",
        "terminal:r1",
        "out:c",
        "out:b",
        "out:a",
    ]


@pytest.mark.asyncio
async def test_chain_short_circuits_on_drop() -> None:
    calls: list[str] = []

    async def terminal(rec: SourceRecord) -> None:
        calls.append("terminal")

    handler = build_handler(
        [_Tag("a", calls), _Drop("b", calls), _Tag("c", calls)],
        terminal,
        _ctx(),
    )
    await handler(_rec("r1"))

    # a opens, b drops, c never runs, terminal never runs, a closes
    assert calls == ["in:a", "drop:b", "out:a"]


@pytest.mark.asyncio
async def test_chain_with_no_middlewares_calls_terminal() -> None:
    calls: list[str] = []

    async def terminal(rec: SourceRecord) -> None:
        calls.append(f"terminal:{rec.external_id}")

    handler = build_handler([], terminal, _ctx())
    await handler(_rec("r1"))

    assert calls == ["terminal:r1"]


@pytest.mark.asyncio
async def test_context_is_propagated_to_every_middleware() -> None:
    capture = _CaptureCtx()

    async def terminal(rec: SourceRecord) -> None:
        pass

    ctx = IngestContext(source_id=99, source_type="x", user_id=7)
    handler = build_handler([capture], terminal, ctx)
    await handler(_rec("r1"))

    assert capture.seen_ctx == ctx
    assert capture.seen_ctx is not None
    assert capture.seen_ctx.source_id == 99


def test_context_is_frozen() -> None:
    """Middlewares no deben poder mutar el contexto compartido."""
    from pydantic import ValidationError

    ctx = IngestContext(source_id=1, source_type="demo", user_id=1)
    with pytest.raises(ValidationError):
        ctx.source_id = 999  # type: ignore[misc]
