"""Core abstractions: SourceRecord, Source, SourceFactory, SourceConfigError,
SourceKind, HealthResult.

The Protocols and one base exception in this module are the only types that
cross between memex and any ingestor. Concrete sources live in
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

  * Every Source is `Source[CursorT]` parameterized by a Pydantic `BaseModel`.
    There is no "cursorless" Source — `fetch` is `(self, checkpoint: CursorT)`
    with no `| None`. The runner constructs `checkpoint_schema()` for a
    fresh source instead of letting the Source see `None`.
  * `kind: ClassVar[SourceKind]` is required — declares which downstream
    modules can consume this source's records (email/chat/social).
  * `payload_schema: ClassVar[type[BasePayload]]` is required — declares the
    Pydantic class describing the records' `payload` shape. Filter rules
    and downstream classifiers introspect it to know what keys exist.
  * `config_schema: ClassVar[type[BaseModel]]` is required — declares what
    the SourceFactory's input dict must validate against. Lets the gateway
    endpoint and CLI validate at the boundary instead of mid-fetch.
  * `checkpoint_schema: ClassVar[type[BaseModel]]` is required — declares
    the cursor shape. The runner does JSONB ↔ CursorT conversion using it.
  * `async health_check() -> HealthResult` is required — lets observability
    surfaces and the operator know if auth/connectivity is broken without
    triggering a full fetch.
  * `advance_checkpoint` returns the same `CursorT`, never a raw dict.

This is what guarantees recovery: any Source can always continue from the
last successfully-flushed checkpoint, because the contract forbids
implementations that ignore the cursor.
"""

from __future__ import annotations

from builtins import type as _type
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from memex.core.payloads import BasePayload

CursorT = TypeVar("CursorT", bound=BaseModel)


class SourceKind(StrEnum):
    """Categoría conceptual de una fuente.

    Determina qué módulos downstream pueden consumir sus records. Cada Source
    declara UNA categoría via `kind: ClassVar[SourceKind]`. Si conceptualmente
    una fuente emite dos (ej. Telegram con chats y canales), se divide en dos
    Sources distintos que comparten cliente como detalle de implementación.

    Categorías iniciales (ampliable a futuro):

    - `EMAIL`: correos con remitente, asunto, cuerpo (IMAP, Gmail, Outlook).
    - `CHAT`: mensajería conversacional con remitente identificado (Telegram
      grupos/supergrupos, WhatsApp, Discord, Slack).
    - `SOCIAL`: broadcast público sin reply esperado (Twitter/X, Mastodon,
      Reddit, canales de Telegram).
    """

    EMAIL = "email"
    CHAT = "chat"
    SOCIAL = "social"


@dataclass(frozen=True)
class HealthResult:
    """Resultado de `Source.health_check()`.

    `status`:
      - `healthy`: la fuente está completamente operativa.
      - `degraded`: parcialmente operativa (ej. rate-limited, lento) — los
        fetches probablemente funcionen pero con métricas peores.
      - `unhealthy`: NO operativa (auth inválida, target unreachable, session
        expirada). Próximos fetches van a fallar.

    `detail` es texto legible para operadores; nunca debe incluir secretos.
    """

    status: Literal["healthy", "degraded", "unhealthy"]
    detail: str
    checked_at: datetime


class SourceConfigError(Exception):
    """Raised when a source-specific config is invalid.

    Concrete sources subclass this (e.g. `ImapConfigError`) so callers can
    catch the generic base and treat any config failure uniformly.
    """


@dataclass(frozen=True)
class MediaBlob:
    """Bytes de un adjunto (imagen/PDF) que el ingestor extrajo, para subir a object storage.

    Viaja en `SourceRecord.media`, SEPARADO del `payload`: nunca se persiste en `inbox.payload`
    (sería un blob enorme en JSONB). El borde de ingest (server-side) lo sube a MinIO
    content-addressed y registra solo la REFERENCIA en `media_assets`; acá viaja como base64
    porque el wire entre ingestor y memex es JSON (ADR-001: el ingestor no toca MinIO ni la DB).
    """

    sha256: str
    content_type: str
    filename: str | None
    size: int
    data_b64: str


@dataclass(frozen=True)
class SourceRecord:
    """The wire envelope that crosses from ingestor to memex.

    `payload` is intentionally `dict[str, Any]` — the storage layer is
    schema-agnostic and JSON travels well over HTTP. The discipline is that
    ingestors CONSTRUCT this dict via the typed Pydantic model that the
    Source declares as `payload_schema` and serialize with
    `.model_dump(mode="json", by_alias=True)`. That way typos at the
    construction site become static type errors instead of runtime KeyErrors
    downstream, and filter rules / classifiers can introspect the schema to
    know what keys exist.

    `media` carries raw attachment bytes (images/PDF) OUT of the ingestor without
    touching MinIO/DB (ADR-001). It defaults to empty: a Source that does not
    extract media is unchanged. The bytes are uploaded + dropped at the ingest
    boundary; they never reach `inbox.payload`.
    """

    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str]
    media: list[MediaBlob] = field(default_factory=list)


@runtime_checkable
class SourceContract(Protocol):
    """Common shape required of every Source — polling or streaming.

    Both `Source[CursorT]` (polling, in `core.source`) and `StreamingSource[CursorT]`
    (event-driven, in `core.streaming`) extend this. The shared attributes are
    what observability surfaces, downstream module dispatchers and filter rules
    need to know without caring about polling vs streaming.

    Not parameterized — the cursor type lives on `Source[CursorT]` /
    `StreamingSource[CursorT]` because those are the ones that operate on the
    cursor in their method signatures.

    All ClassVars are required — mypy strict raises a missing-attribute error
    if a subclass omits any of them.
    """

    type: ClassVar[str]
    """Source-type slug — matches `sources.type` in the DB and the registry key."""

    kind: ClassVar[SourceKind]
    """Conceptual category — drives which downstream modules consume this source."""

    payload_schema: ClassVar[_type[BasePayload]]
    """Pydantic class that describes the shape of records' `payload`."""

    config_schema: ClassVar[_type[BaseModel]]
    """Pydantic class that `SourceFactory`'s input dict must validate against."""

    checkpoint_schema: ClassVar[_type[BaseModel]]
    """Pydantic class for the cursor — runner uses it to (de)serialize JSONB."""

    async def health_check(self) -> HealthResult:
        """Check the source's operational health without fetching.

        Typical implementations: verify auth credentials are valid, target is
        reachable, session file is still authenticated. Should complete in
        seconds, not minutes. Used by `GET /sources/{id}/health` and by
        observability dashboards.

        Must never raise — convert any error to `HealthResult(status="unhealthy",
        detail=str(error), checked_at=now)`.
        """
        ...


@runtime_checkable
class Source(SourceContract, Protocol[CursorT]):
    """Polling source — fetched in cron-style cycles by the runner.

    Generic in `CursorT` so mypy verifies the cursor flow end-to-end: a
    `Source[ImapCursor]` receives and returns `ImapCursor`, not `dict`. If
    a concrete Source's `fetch` declares `checkpoint: dict` instead of the
    parameterized type, mypy raises a signature-mismatch error.
    """

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
