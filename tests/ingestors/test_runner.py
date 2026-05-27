from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import MagicMock

from memex.core.source import SourceRecord
from memex.ingestors.runner import run_ingestor


class FakeSource:
    type: ClassVar[str] = "fake"

    def __init__(self, records: list[SourceRecord]) -> None:
        self._records = records
        self.last_checkpoint_seen: Any = "<unset>"

    def fetch(self, checkpoint: dict[str, Any] | None) -> Iterable[SourceRecord]:
        self.last_checkpoint_seen = checkpoint
        yield from self._records

    def advance_checkpoint(
        self, checkpoint: dict[str, Any] | None, last: SourceRecord
    ) -> dict[str, Any]:
        return {"last_external_id": last.external_id}


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
    }

    stats = run_ingestor(source, source_id=42, sink=client, chunk_size=10, chunk_sleep_ms=0)

    assert stats.posted == 3
    assert stats.inserted == 3
    client.post_ingest_batch.assert_called_once()
    posted = client.post_ingest_batch.call_args[0][0]
    assert [r["external_id"] for r in posted] == ["e0", "e1", "e2"]
    assert all(r["source_id"] == 42 for r in posted)
    client.put_checkpoint.assert_called_once_with(42, {"last_external_id": "e2"})


def test_run_ingestor_multiple_chunks_advances_after_each() -> None:
    records = [_record(f"e{i}") for i in range(5)]
    source = FakeSource(records)
    client = MagicMock()
    client.get_checkpoint.return_value = {"existing": True}
    client.post_ingest_batch.return_value = {
        "inserted": 2,
        "duplicates": 0,
        "errors": 0,
    }

    run_ingestor(source, source_id=1, sink=client, chunk_size=2, chunk_sleep_ms=0)

    # Chunks: [e0,e1], [e2,e3], [e4] → 3 flushes
    assert client.post_ingest_batch.call_count == 3
    assert client.put_checkpoint.call_count == 3
    advancement_calls = [c[0] for c in client.put_checkpoint.call_args_list]
    assert advancement_calls[-1] == (1, {"last_external_id": "e4"})


def test_run_ingestor_aggregates_stats_across_chunks() -> None:
    records = [_record(f"e{i}") for i in range(4)]
    source = FakeSource(records)
    client = MagicMock()
    client.get_checkpoint.return_value = None
    client.post_ingest_batch.side_effect = [
        {"inserted": 2, "duplicates": 0, "errors": 0},
        {"inserted": 1, "duplicates": 1, "errors": 0},
    ]

    stats = run_ingestor(source, source_id=1, sink=client, chunk_size=2, chunk_sleep_ms=0)

    assert stats.posted == 4
    assert stats.inserted == 3
    assert stats.duplicates == 1
    assert stats.errors == 0


def test_run_ingestor_passes_loaded_checkpoint_to_source() -> None:
    source = FakeSource([])
    client = MagicMock()
    client.get_checkpoint.return_value = {"folders": {"INBOX": {"uid": 5}}}

    run_ingestor(source, source_id=1, sink=client)

    assert source.last_checkpoint_seen == {"folders": {"INBOX": {"uid": 5}}}
