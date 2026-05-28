"""Core abstractions: SourceRecord, Source, SourceFactory, SourceConfigError.

The two Protocols and one base exception in this module are the only types
that cross between memex and any ingestor. Concrete sources live in
`memex.ingestors.<type>/` and depend only on this module + `memex.logging`.

The discipline (enforced by tests/test_typing_discipline.py):

  * Code that consumes a "source" types against `Source`, never against a
    concrete class like `ImapSource`.
  * Code that builds a source from a config dict types against
    `SourceFactory`, never against a concrete constructor.
  * Code that catches config errors catches `SourceConfigError`, never the
    source-specific subclass.

This is what lets us add a new ingestor (Telegram, social, ...) without
touching anything that already works.

Contract guarantees (enforced by mypy strict):

  * A Source is `Source[CursorT]` parameterized by a Pydantic `BaseModel`.
    There is no "cursorless" Source — `fetch` is `(self, checkpoint: CursorT)`
    with no `| None`. The runner constructs `checkpoint_schema()` for a
    fresh source instead of letting the Source see `None`.
  * `checkpoint_schema` is a `ClassVar[type[BaseModel]]` that every concrete
    Source must declare. mypy errors if missing on a `Source[...]` subclass.
  * `advance_checkpoint` returns the same `CursorT`, never a raw dict — the
    runner does the JSONB ↔ CursorT conversion at the wire boundary.

This is what guarantees recovery: any Source can always continue from the
last successfully-flushed checkpoint, because the contract forbids
implementations that ignore the cursor.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

CursorT = TypeVar("CursorT", bound=BaseModel)


class SourceConfigError(Exception):
    """Raised when a source-specific config is invalid.

    Concrete sources subclass this (e.g. `ImapConfigError`) so callers can
    catch the generic base and treat any config failure uniformly.
    """


@dataclass(frozen=True)
class SourceRecord:
    """The wire envelope that crosses from ingestor to memex.

    `payload` is intentionally `dict[str, Any]` — the storage layer is
    schema-agnostic and JSON travels well over HTTP. The discipline is that
    ingestors CONSTRUCT this dict via a typed Pydantic model
    (e.g. `EmailPayload` in `memex.core.payloads`) and serialize with
    `.model_dump(mode="json", by_alias=True)`. That way typos at the
    construction site become static type errors instead of runtime KeyErrors
    downstream.
    """

    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str]


@runtime_checkable
class Source(Protocol[CursorT]):
    """Anything memex can pull data from.

    `type` is the string under which this source is registered (matches
    `sources.type` in the DB). `checkpoint_schema` is the Pydantic class
    that describes the cursor shape — declared once per Source and used by
    the runner to (de)serialize the JSONB row in `source_checkpoints`.
    `fetch` yields records lazily; the runner chunks them.
    `advance_checkpoint` updates the cursor given the last successfully
    posted record.

    Generic in `CursorT` so mypy verifies the cursor flow end-to-end: a
    `Source[ImapCursor]` receives and returns `ImapCursor`, not `dict`. If
    a concrete Source's `fetch` declares `checkpoint: dict` instead of the
    parameterized type, mypy raises a signature-mismatch error.
    """

    type: ClassVar[str]
    checkpoint_schema: ClassVar[_type[BaseModel]]

    def fetch(self, checkpoint: CursorT) -> Iterable[SourceRecord]: ...

    def advance_checkpoint(self, checkpoint: CursorT, last: SourceRecord) -> CursorT: ...


@runtime_checkable
class SourceFactory(Protocol):
    """Callable that builds a `Source` from a raw config dict.

    Each ingestor module exports a `make_source(cfg)` function matching this
    Protocol. The registry (`memex.sources.resolve`) returns one of these for
    a given source type string.

    Returns `Source[Any]` because the factory is invoked behind a string-keyed
    registry — the caller (the runner) recovers the cursor type at runtime
    via `source.checkpoint_schema`.
    """

    def __call__(self, cfg: dict[str, Any]) -> Source[Any]: ...
