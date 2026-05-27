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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol, runtime_checkable


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
class Source(Protocol):
    """Anything memex can pull data from.

    `type` is the string under which this source is registered (matches
    `sources.type` in the DB). `fetch` yields records lazily; the runner
    chunks them. `advance_checkpoint` updates the cursor given the last
    successfully posted record.
    """

    type: ClassVar[str]

    def fetch(self, checkpoint: dict[str, Any] | None) -> Iterable[SourceRecord]: ...

    def advance_checkpoint(
        self, checkpoint: dict[str, Any] | None, last: SourceRecord
    ) -> dict[str, Any]: ...


@runtime_checkable
class SourceFactory(Protocol):
    """Callable that builds a `Source` from a raw config dict.

    Each ingestor module exports a `make_source(cfg)` function matching this
    Protocol. The registry (`memex.sources.resolve`) returns one of these for
    a given source type string.
    """

    def __call__(self, cfg: dict[str, Any]) -> Source: ...
