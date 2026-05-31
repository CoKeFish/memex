from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MediaItem(BaseModel):
    """Bytes de un adjunto imagen/PDF que cruzan el wire (base64) para subir a MinIO.

    Espeja `memex.core.source.MediaBlob`. Default-vacío en `IngestRequest.media` → requests sin
    adjuntos quedan idénticos al contrato previo (backward-compatible).
    """

    sha256: str
    content_type: str
    filename: str | None = None
    size: int
    data_b64: str


class IngestRequest(BaseModel):
    source_id: int
    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str] = Field(default_factory=list)
    media: list[MediaItem] = Field(default_factory=list)


class IngestResponse(BaseModel):
    inserted: bool | None = None
    id: int | None = None
    reason: str | None = None
    would_insert: bool | None = None
    validations: dict[str, Any] | None = None


class IngestBatchRequest(BaseModel):
    records: list[IngestRequest]


class IngestBatchResponse(BaseModel):
    inserted: int
    duplicates: int
    errors: int
    filtered: int


class GatewayRecord(BaseModel):
    """Record que viaja al gateway — sin source_id (lo resuelve el gateway desde el URL)."""

    external_id: str
    occurred_at: datetime
    payload: dict[str, Any]
    dedupe_keys: list[str] = Field(default_factory=list)


class GatewayStateRequest(BaseModel):
    source_type: str


class GatewayStateResponse(BaseModel):
    source_id: int
    cursor: dict[str, Any] | None = None
    created: bool


class GatewayCursorRequest(BaseModel):
    cursor: dict[str, Any]


class GatewayPluginIngestRequest(BaseModel):
    records: list[GatewayRecord]


class GatewayIngestStats(BaseModel):
    source_id: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int


class ClassificationInfo(BaseModel):
    tier: str
    metadata: dict[str, Any] | None = None


class SummaryInfo(BaseModel):
    id: int | None = None
    tier: str
    content: str
    created_at: datetime | None = None


class ExtractionInfo(BaseModel):
    # `done` puede ser True con listas vacías: el cursor marca "procesado, sin datos relevantes".
    done: bool = False
    modules: list[str] = Field(default_factory=list)
    finance: list[dict[str, Any]] = Field(default_factory=list)
    calendar: list[dict[str, Any]] = Field(default_factory=list)


class LlmCallInfo(BaseModel):
    """Una llamada LLM atribuida a este mensaje (traza de auditoría)."""

    purpose: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    status: str
    created_at: datetime | None = None
    # Decisión de la fase: ruteo {slugs_in, chosen, ...}; extracción {items, discarded, ...}.
    metadata: dict[str, Any] | None = None


class LlmUsageInfo(BaseModel):
    calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    items: list[LlmCallInfo] = Field(default_factory=list)


class InboxRow(BaseModel):
    id: int
    source_id: int
    external_id: str
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, Any]
    processed_at: datetime | None
    process_error: str | None
    attempts: int
    # Solo los puebla el detalle (GET /inbox/{id}); en la lista viajan como null.
    classification: ClassificationInfo | None = None
    summary: SummaryInfo | None = None
    extraction: ExtractionInfo | None = None
    llm: LlmUsageInfo | None = None


class ProcessResponse(BaseModel):
    """Resultado de procesar (clasificar) un mensaje puntual."""

    inbox_id: int
    tier: str
    reason: str
    classified: bool  # True si se clasificó ahora; False si ya estaba
    already: bool


class SummarizeResponse(BaseModel):
    """Resultado de resumir un mensaje o su ventana (+ costo de la corrida)."""

    status: str  # ok | already | skipped
    messages: int = 0
    id: int | None = None
    tier: str | None = None
    content: str | None = None
    created_at: datetime | None = None
    calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ExtractResponse(BaseModel):
    """Resultado de extraer (módulos) sobre un mensaje o su ventana (+ costo + detalle)."""

    status: str  # ok | no_modules
    items: int = 0
    discarded: int = 0
    by_module: dict[str, int] = Field(default_factory=dict)
    done: bool = False
    modules: list[str] = Field(default_factory=list)
    finance: list[dict[str, Any]] = Field(default_factory=list)
    calendar: list[dict[str, Any]] = Field(default_factory=list)
    calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class InboxList(BaseModel):
    items: list[InboxRow]
    next_cursor: int | None = None


class StatsBySource(BaseModel):
    total: int
    pending: int
    errored: int


class InboxStats(BaseModel):
    sources: dict[int, StatsBySource]


class SourceCreate(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class SourceRow(BaseModel):
    id: int
    user_id: int
    name: str
    type: str
    enabled: bool
    config: dict[str, Any]
    created_at: datetime


class CheckpointBody(BaseModel):
    cursor: dict[str, Any]


class FetchResponse(BaseModel):
    """Resultado de una corrida de fetch a demanda (`POST /sources/{id}/fetch`).

    En dry-run los contadores son lo que PASARÍA (sin escribir): `inserted` = nuevos,
    `duplicates` = ya existentes ignorados, `filtered` = descartados por filter_rules.
    `posted` = total escaneado que cruzó el wire.
    """

    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    dry_run: bool
    ms_elapsed: int
