"""Contract tests for `memex.core.source.Source[CursorT]`.

These verify the runtime properties of the refactored Protocol. The static
guarantees (cursor not optional, signature matches, etc.) are enforced by
mypy strict — see comments in `tests/test_typing_discipline.py` and the
docstring of `memex.core.source` for the rules mypy applies.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from memex.core.source import Source, SourceRecord


class _DemoCursor(BaseModel):
    """A toy cursor used to validate that the contract holds for any BaseModel."""

    seq: int = 0
    seen: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class _DemoSource:
    """Concrete Source[_DemoCursor] that uses the cursor properly."""

    type: ClassVar[str] = "demo"
    checkpoint_schema: ClassVar[_type[BaseModel]] = _DemoCursor

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records
        self.last_seen_checkpoint: _DemoCursor | None = None

    def fetch(self, checkpoint: _DemoCursor) -> Iterable[SourceRecord]:
        self.last_seen_checkpoint = checkpoint
        yield from self._records

    def advance_checkpoint(self, checkpoint: _DemoCursor, last: SourceRecord) -> _DemoCursor:
        return _DemoCursor(
            seq=checkpoint.seq + 1,
            seen=[*checkpoint.seen, last.external_id],
        )


def _rec(eid: str) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 27, 0, 0, tzinfo=UTC),
        payload={"eid": eid},
        dedupe_keys=[f"k:{eid}"],
    )


def test_concrete_source_satisfies_protocol_at_runtime() -> None:
    """A class that fulfills the Protocol structurally passes isinstance check."""
    src = _DemoSource([])
    assert isinstance(src, Source)


def test_source_receives_typed_cursor_not_dict() -> None:
    """The cursor that arrives in fetch is the declared BaseModel subclass."""
    src = _DemoSource([_rec("a")])
    cursor = _DemoCursor(seq=5, seen=["prev"])
    list(src.fetch(cursor))
    assert src.last_seen_checkpoint is cursor
    assert isinstance(src.last_seen_checkpoint, _DemoCursor)


def test_advance_checkpoint_returns_typed_cursor() -> None:
    """advance_checkpoint preserves type — never degrades to dict."""
    src = _DemoSource([])
    cursor = _DemoCursor()
    new_cursor = src.advance_checkpoint(cursor, _rec("e1"))
    assert isinstance(new_cursor, _DemoCursor)
    assert new_cursor.seq == 1
    assert new_cursor.seen == ["e1"]


def test_checkpoint_schema_is_declared_class_attribute() -> None:
    """The runner reads `checkpoint_schema` to validate JSONB rows. It must
    be a class attribute (not an instance attribute), pointing at a BaseModel
    subclass."""
    assert _DemoSource.checkpoint_schema is _DemoCursor
    assert issubclass(_DemoSource.checkpoint_schema, BaseModel)


def test_default_cursor_constructs_from_empty() -> None:
    """The runner instantiates `checkpoint_schema()` (no args) when there is
    no prior checkpoint. Every cursor type must be constructible with all
    defaults, otherwise a fresh source can't bootstrap."""
    cursor = _DemoSource.checkpoint_schema()
    assert isinstance(cursor, _DemoCursor)
    assert cursor.seq == 0
    assert cursor.seen == []
