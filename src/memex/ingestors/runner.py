from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel

from memex.core.sink import MemexSink
from memex.core.source import Source, SourceRecord
from memex.logging import get_logger


@dataclass
class RunStats:
    posted: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0
    filtered: int = 0
    ms_elapsed: int = 0
    # Costo de APIs externas pagas de la corrida (hoy: runs de actor de Apify). Lo setea
    # el BORDE (fetch_runner / CLI) tras drenar `ActorRunReporting` — nunca `run_ingestor`:
    # si el sink falla a mitad de corrida estas stats no se devuelven, pero el gasto ya
    # ocurrió y se persiste aparte en un `finally`.
    api_cost_usd: float | None = None


def _record_to_request(record: SourceRecord, source_id: int) -> dict[str, Any]:
    req: dict[str, Any] = {
        "source_id": source_id,
        "external_id": record.external_id,
        "occurred_at": record.occurred_at.isoformat(),
        "payload": record.payload,
        "dedupe_keys": list(record.dedupe_keys),
    }
    # `media` viaja solo si el ingestor extrajo bytes (default vacío → request idéntico al previo).
    if record.media:
        req["media"] = [asdict(m) for m in record.media]
    return req


def run_ingestor(
    source: Source[Any],
    source_id: int,
    sink: MemexSink,
    *,
    chunk_size: int = 20,
    chunk_sleep_ms: int = 100,
) -> RunStats:
    """Drive a Source through memex's API.

    Types against the `Source` and `MemexSink` Protocols — never against a
    concrete class. That lets us swap the transport (HTTP today, in-process
    or queue-backed tomorrow) without touching the runner.

    Idempotent on failure: if any sink call raises, the checkpoint is left at
    the last successfully-flushed position. The next run re-fetches the
    affected records and memex deduplicates via UNIQUE(source_id, external_id).

    Per-entity checkpoint advance: after a chunk is flushed, `advance_checkpoint`
    is folded over EVERY record in the chunk (not just the last). For multi-entity
    cursors (IMAP folders, social accounts) this means each entity advances to its
    own latest flushed record in a single pass — a chunk that mixes accounts no
    longer leaves the non-last accounts un-advanced. Safe because records arrive
    ascending within each entity, so the fold lands each entity on its max; and the
    fold runs only after the chunk posted successfully, preserving the idempotency
    guarantee above.

    Adapter at the wire boundary: the sink stores cursors as JSONB (`dict`),
    but the Source contract requires a typed `CursorT`. The runner is the
    only place that does the dict ↔ CursorT conversion — Sources never see
    `dict` or `None`. If a Source has no prior checkpoint, the runner
    constructs `checkpoint_schema()` (Pydantic defaults) and passes that.
    """
    log = get_logger("memex.ingestors.runner").bind(source_id=source_id)
    stats = RunStats()
    started = time.monotonic()

    cursor_raw = sink.get_checkpoint(source_id) or {}
    checkpoint: BaseModel = source.checkpoint_schema.model_validate(cursor_raw)
    chunk: list[SourceRecord] = []
    chunk_index = 0

    def flush() -> None:
        nonlocal checkpoint, chunk_index
        if not chunk:
            return
        payload = [_record_to_request(r, source_id) for r in chunk]
        result = sink.post_ingest_batch(payload)
        stats.posted += len(chunk)
        stats.inserted += int(result.get("inserted", 0))
        stats.duplicates += int(result.get("duplicates", 0))
        stats.errors += int(result.get("errors", 0))
        stats.filtered += int(result.get("filtered", 0))
        # Fold sobre TODO el chunk (no solo el último): cada entidad del cursor
        # (carpeta IMAP / cuenta social) avanza a su propio último record flusheado.
        for record in chunk:
            checkpoint = source.advance_checkpoint(checkpoint, record)
        sink.put_checkpoint(source_id, checkpoint.model_dump(mode="json"))
        log.info(
            "ingestor.chunk.flushed",
            chunk_index=chunk_index,
            chunk_size=len(chunk),
            inserted=result.get("inserted"),
            duplicates=result.get("duplicates"),
            errors=result.get("errors"),
            filtered=result.get("filtered"),
        )
        chunk_index += 1
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
