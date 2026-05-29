from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock

from pydantic import BaseModel, ConfigDict, Field

from memex.core.payloads import BasePayload
from memex.core.source import HealthResult, SourceKind, SourceRecord
from memex.ingestors.runner import run_ingestor


class FakeRunnerCursor(BaseModel):
    last_external_id: str | None = None
    seen: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class FakeRunnerPayload(BasePayload):
    eid: str


class FakeRunnerConfig(BaseModel):
    name: str = "fake"


class FakeSource:
    type: ClassVar[str] = "fake"
    kind: ClassVar[SourceKind] = SourceKind.EMAIL
    payload_schema: ClassVar[_type[BasePayload]] = FakeRunnerPayload
    config_schema: ClassVar[_type[BaseModel]] = FakeRunnerConfig
    checkpoint_schema: ClassVar[_type[BaseModel]] = FakeRunnerCursor

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="fake", checked_at=datetime.now(UTC))

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records
        self.last_checkpoint_seen: FakeRunnerCursor | None = None

    def fetch(self, checkpoint: FakeRunnerCursor) -> Iterable[SourceRecord]:
        self.last_checkpoint_seen = checkpoint
        yield from self._records

    def advance_checkpoint(
        self, checkpoint: FakeRunnerCursor, last: SourceRecord
    ) -> FakeRunnerCursor:
        return FakeRunnerCursor(
            last_external_id=last.external_id,
            seen=[*checkpoint.seen, last.external_id],
        )


def _record(eid: str) -> SourceRecord:
    return SourceRecord(
        external_id=eid,
        occurred_at=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
        payload={"eid": eid},
        dedupe_keys=[f"k:{eid}"],
    )


def test_run_ingestor_with_no_records_does_no_work() -> None:
    source = FakeSource([])
    client = MagicMock()
    client.get_checkpoint.return_value = None

    stats = run_ingestor(source, source_id=1, sink=client)

    assert stats.posted == 0
    assert stats.inserted == 0
    assert stats.duplicates == 0
    assert stats.errors == 0
    assert stats.filtered == 0
    client.post_ingest_batch.assert_not_called()
    client.put_checkpoint.assert_not_called()


def test_run_ingestor_single_chunk_posts_and_advances() -> None:
    records = [_record(f"e{i}") for i in range(3)]
    source = FakeSource(records)
    client = MagicMock()
    client.get_checkpoint.return_value = None
    client.post_ingest_batch.return_value = {
        "inserted": 3,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
    }

    stats = run_ingestor(source, source_id=42, sink=client, chunk_size=10, chunk_sleep_ms=0)

    assert stats.posted == 3
    assert stats.inserted == 3
    assert stats.filtered == 0
    client.post_ingest_batch.assert_called_once()
    posted = client.post_ingest_batch.call_args[0][0]
    assert [r["external_id"] for r in posted] == ["e0", "e1", "e2"]
    assert all(r["source_id"] == 42 for r in posted)
    # advance_checkpoint is invoked once per flush with the LAST record of the
    # chunk — so only "e2" lands in `seen`, not the full batch.
    client.put_checkpoint.assert_called_once_with(42, {"last_external_id": "e2", "seen": ["e2"]})


def test_run_ingestor_multiple_chunks_advances_after_each() -> None:
    records = [_record(f"e{i}") for i in range(5)]
    source = FakeSource(records)
    client = MagicMock()
    client.get_checkpoint.return_value = {"last_external_id": None, "seen": []}
    client.post_ingest_batch.return_value = {
        "inserted": 2,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
    }

    run_ingestor(source, source_id=1, sink=client, chunk_size=2, chunk_sleep_ms=0)

    # Chunks: [e0,e1], [e2,e3], [e4] → 3 flushes
    assert client.post_ingest_batch.call_count == 3
    assert client.put_checkpoint.call_count == 3
    # Three flushes: ["e0","e1"] -> last="e1", ["e2","e3"] -> last="e3",
    # ["e4"] -> last="e4". seen accumulates only the LAST of each chunk.
    final_call = client.put_checkpoint.call_args_list[-1][0]
    assert final_call == (1, {"last_external_id": "e4", "seen": ["e1", "e3", "e4"]})


def test_run_ingestor_aggregates_stats_across_chunks() -> None:
    records = [_record(f"e{i}") for i in range(4)]
    source = FakeSource(records)
    client = MagicMock()
    client.get_checkpoint.return_value = None
    # Realista: lo que el runner postea (len del chunk) lo reparte la API entre
    # inserted/duplicates/errors/filtered. Chunk 1: 1 insertado + 1 dropeado;
    # chunk 2: 1 insertado + 1 duplicado.
    client.post_ingest_batch.side_effect = [
        {"inserted": 1, "duplicates": 0, "errors": 0, "filtered": 1},
        {"inserted": 1, "duplicates": 1, "errors": 0, "filtered": 0},
    ]

    stats = run_ingestor(source, source_id=1, sink=client, chunk_size=2, chunk_sleep_ms=0)

    assert stats.posted == 4
    assert stats.inserted == 2
    assert stats.duplicates == 1
    assert stats.errors == 0
    assert stats.filtered == 1
    # Invariante: posted = inserted + duplicates + errors + filtered.
    assert stats.posted == stats.inserted + stats.duplicates + stats.errors + stats.filtered


def test_run_ingestor_passes_loaded_checkpoint_typed_to_source() -> None:
    """Runner deserializes the JSONB dict into the source's checkpoint_schema
    before passing it to fetch(). The source never sees `dict` or `None`."""
    source = FakeSource([])
    client = MagicMock()
    client.get_checkpoint.return_value = {"last_external_id": "prev-99", "seen": ["prev-99"]}

    run_ingestor(source, source_id=1, sink=client)

    assert source.last_checkpoint_seen is not None
    assert isinstance(source.last_checkpoint_seen, FakeRunnerCursor)
    assert source.last_checkpoint_seen.last_external_id == "prev-99"
    assert source.last_checkpoint_seen.seen == ["prev-99"]


def test_run_ingestor_passes_default_cursor_when_none_persisted() -> None:
    """A fresh source (no row in source_checkpoints) gets schema() defaults,
    not None — guaranteed by the contract."""
    source = FakeSource([])
    client = MagicMock()
    client.get_checkpoint.return_value = None

    run_ingestor(source, source_id=1, sink=client)

    assert source.last_checkpoint_seen is not None
    assert isinstance(source.last_checkpoint_seen, FakeRunnerCursor)
    assert source.last_checkpoint_seen.last_external_id is None
    assert source.last_checkpoint_seen.seen == []
