from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from memex.core.source import Source, SourceRecord
from memex.ingestors.http_client import MemexClient
from memex.logging import get_logger


@dataclass
class RunStats:
    posted: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0
    ms_elapsed: int = 0


def _record_to_request(record: SourceRecord, source_id: int) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "external_id": record.external_id,
        "occurred_at": record.occurred_at.isoformat(),
        "payload": record.payload,
        "dedupe_keys": list(record.dedupe_keys),
    }


def run_ingestor(
    source: Source,
    source_id: int,
    client: MemexClient,
    *,
    chunk_size: int = 20,
    chunk_sleep_ms: int = 100,
) -> RunStats:
    """Drive a Source through memex's HTTP API.

    Reads the source's checkpoint from memex, iterates records produced by
    `source.fetch`, posts in chunks of `chunk_size` to `/ingest/batch`,
    advances the checkpoint after each successful chunk via
    `source.advance_checkpoint`, and persists the cursor back to memex.

    Idempotent on failure: if any HTTP call raises, the checkpoint is left
    at the last successfully-flushed position. The next run re-fetches the
    affected records and memex deduplicates via UNIQUE(source_id, external_id).
    """
    log = get_logger("memex.ingestors.runner").bind(source_id=source_id)
    stats = RunStats()
    started = time.monotonic()

    checkpoint: dict[str, Any] | None = client.get_checkpoint(source_id)
    chunk: list[SourceRecord] = []

    def flush() -> None:
        nonlocal checkpoint
        if not chunk:
            return
        last_record = chunk[-1]
        payload = [_record_to_request(r, source_id) for r in chunk]
        result = client.post_ingest_batch(payload)
        stats.posted += len(chunk)
        stats.inserted += int(result.get("inserted", 0))
        stats.duplicates += int(result.get("duplicates", 0))
        stats.errors += int(result.get("errors", 0))
        checkpoint = source.advance_checkpoint(checkpoint, last_record)
        client.put_checkpoint(source_id, checkpoint)
        log.info(
            "chunk_flushed",
            chunk_size=len(chunk),
            inserted=result.get("inserted"),
            duplicates=result.get("duplicates"),
            errors=result.get("errors"),
        )
        chunk.clear()
        if chunk_sleep_ms > 0:
            time.sleep(chunk_sleep_ms / 1000.0)

    for record in source.fetch(checkpoint):
        chunk.append(record)
        if len(chunk) >= chunk_size:
            flush()

    flush()

    stats.ms_elapsed = int((time.monotonic() - started) * 1000)
    return stats
