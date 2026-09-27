"""MemexSink Protocol — the contract any "thing that talks to memex" must satisfy.

Why a Protocol instead of using `MemexServerClient` directly: code that consumes
this contract (notably `run_ingestor`) types against the Protocol, not the
concrete HTTP client. That means:

  * Tests can pass a fake sink without spinning HTTP fixtures.
  * Future transports (e.g. an in-process call when ingestor and API are
    colocated, or a gRPC client, or a queue producer) drop in without
    touching the consumer.
  * The `MemexServerClient` class is one valid implementation, not THE only one.

The discipline is enforced by lint (see tests/test_typing_discipline.py):
modules that orchestrate ingestion must reference `MemexSink` in annotations,
never `MemexServerClient` directly.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemexSink(Protocol):
    """Anything that can list sources, manage checkpoints, and ingest batches."""

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        """List enabled sources of the given type."""
        ...

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        """Return the current cursor for a source, or None if never set."""
        ...

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        """Persist a new cursor for a source."""
        ...

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        """Submit a batch of records. Returns counters: inserted, duplicates, errors, filtered."""
        ...
