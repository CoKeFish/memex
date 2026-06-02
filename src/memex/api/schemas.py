from datetime import date, datetime
from typing import Any, Literal

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


class ClassifyRequest(BaseModel):
    """Override manual del tier de un mensaje (aplicado de inmediato)."""

    tier: Literal["blacklist", "batch", "individual"]


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

    # `request_id` agrupa las llamadas de una misma corrida HTTP (un "Procesar"); las corridas
    # batch/CLI lo dejan en None y el front las agrupa por cercanía temporal.
    request_id: str | None = None
    purpose: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    status: str
    error_message: str | None = None
    created_at: datetime | None = None
    # Decisión de la fase: ruteo {slugs_in, chosen, ...}; extracción {items, discarded, ...}.
    metadata: dict[str, Any] | None = None


class LlmUsageInfo(BaseModel):
    calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    items: list[LlmCallInfo] = Field(default_factory=list)


class MediaAssetInfo(BaseModel):
    """Un adjunto del mensaje (media_assets): referencia + estado/texto de OCR.

    El blob NO viaja acá (solo la referencia content-addressed); se sirve aparte por
    `GET /media/{id}`. `ocr_model` codifica la ruta del PDF (`pymupdf-text` = solo capa de texto;
    `pymupdf+<modelo>` = texto + visión; `pymupdf-raster+<modelo>` = escaneado) y del ZIP
    (`zip-text` / `zip+<modelo>`). El detalle de qué imágenes se OCR-earon / omitieron vive en la
    traza `llm` (llamadas `purpose='ocr'`).
    """

    id: int
    sha256: str
    content_type: str
    filename: str | None = None
    extension: str | None = None
    size_bytes: int
    ocr_status: str  # pending | ok | error | skipped
    ocr_model: str | None = None
    ocr_text: str | None = None
    ocr_error: str | None = None
    ocr_attempts: int = 0
    ocr_done_at: datetime | None = None


class FeedbackInfo(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str = "open"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FeedbackRequest(BaseModel):
    """Feedback rápido del usuario sobre un mensaje (categorías + nota). Solo captura."""

    kinds: list[str] = Field(default_factory=list)
    note: str | None = None


class FeedbackListItem(FeedbackInfo):
    """Feedback + contexto del mensaje, para inspección (GET /feedback)."""

    inbox_id: int
    subject: str | None = None
    from_email: str | None = None
    tier: str | None = None


class FeedbackList(BaseModel):
    items: list[FeedbackListItem] = Field(default_factory=list)


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
    # Tier + avance del pipeline: van TANTO en la lista (indicadores por fila) como en el detalle.
    classification: ClassificationInfo | None = None
    summarized: bool = False
    extracted: bool = False
    # Objetos completos: solo los puebla el detalle (GET /inbox/{id}); en la lista van vacíos/null.
    summary: SummaryInfo | None = None
    extraction: ExtractionInfo | None = None
    llm: LlmUsageInfo | None = None
    media: list[MediaAssetInfo] = Field(default_factory=list)
    # Feedback manual del usuario sobre este mensaje — solo en el detalle.
    feedback: FeedbackInfo | None = None


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


class ReprocessRequest(BaseModel):
    """Etapas a re-aplicar a un mensaje. `force` reprocesa lo ya hecho (invalida cursores)."""

    stages: list[str] = Field(default_factory=list)
    force: bool = False


class ReprocessResponse(BaseModel):
    """Resultado de un reproceso por mensaje: objetivos, etapas corridas y el detalle por etapa."""

    targets: int
    stages: list[str]
    results: dict[str, Any] = Field(default_factory=dict)


FilterActionLiteral = Literal["keep", "ignore", "archive"]


class FilterRuleInfo(BaseModel):
    """Una regla de `filter_rules` expuesta al dashboard (sin `user_id`)."""

    id: int
    source_type: str | None = None
    source_id: int | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    action: str
    priority: int
    enabled: bool


class FilterRuleCreate(BaseModel):
    source_type: str | None = None
    source_id: int | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    action: FilterActionLiteral = "ignore"
    priority: int = 100
    enabled: bool = True


class FilterRuleUpdate(BaseModel):
    scope: dict[str, Any] | None = None
    action: FilterActionLiteral | None = None
    priority: int | None = None
    enabled: bool | None = None


class FilterRuleList(BaseModel):
    items: list[FilterRuleInfo] = Field(default_factory=list)


class InboxList(BaseModel):
    items: list[InboxRow]
    next_cursor: int | None = None


class StatsBySource(BaseModel):
    total: int
    pending: int
    errored: int


class InboxStats(BaseModel):
    sources: dict[int, StatsBySource]


class FinanceExpenseRow(BaseModel):
    """Un gasto extraído (fila de `mod_finance_expenses`).

    Espeja `memex.modules.finance.schema.ExpenseItem` más las columnas de la tabla. `amount` cruza
    como `float` (la DB es NUMERIC(14,2)) siguiendo la convención del repo para dinero en respuestas
    (cf. `cost_usd`). `occurred_on` puede ser NULL cuando el LLM no pudo fechar el cargo.
    """

    id: int
    amount: float
    currency: str
    category: str
    merchant: str
    occurred_on: date | None
    description: str
    evidence: str
    source_inbox_ids: list[int]
    created_at: datetime


class FinanceExpenseList(BaseModel):
    items: list[FinanceExpenseRow]
    next_cursor: int | None = None


# ---- Métricas de costo LLM (tabla `llm_calls`) --------------------------------------------------
# La vista /metricas agrega server-side (GROUP BY) sobre llm_calls: cortes por fuente, por módulo
# (de `purpose`), por modelo, matriz fuente x módulo y serie diaria. `untabulated` se deriva
# de los datos (tokens>0 con cost_usd=0 → modelo sin precio tabulado, gasto silencioso a señalar).


class LlmKpis(BaseModel):
    """KPIs del rango: costo, llamadas, tokens, eficiencia de cache y errores."""

    cost_usd: float
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_hit_ratio: float  # cache_hit_tokens / prompt_tokens (0..1); 0 si no hay prompt tokens
    avg_cost_usd: float
    avg_latency_ms: float  # promedio de status='ok' (excluye filtered/error con latencia 0)
    errors: int
    # Costo y #llamadas del periodo anterior de igual longitud (para la variación %); None si no hay
    # `since`. prev_calls distingue "periodo previo sin datos" (0) de "creció mucho" en el front.
    prev_cost_usd: float | None = None
    prev_calls: int | None = None


class LlmBySource(BaseModel):
    """Costo por fuente. `source_id` None = bucket sin source; el label distingue (calendar)."""

    source_id: int | None
    source_name: str
    calls: int
    tokens: int
    cost_usd: float


class LlmByModule(BaseModel):
    """Costo por módulo/etapa (derivado de `purpose`)."""

    module: str
    calls: int
    tokens: int
    cost_usd: float


class LlmByModel(BaseModel):
    """Costo por modelo. `untabulated` = tokens>0 pero cost_usd=0 (precio no tabulado)."""

    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    untabulated: bool


class LlmSourceModuleCell(BaseModel):
    """Una celda de la matriz fuente x módulo."""

    source_id: int | None
    source_name: str
    module: str
    calls: int
    cost_usd: float


class LlmDailyPoint(BaseModel):
    """Costo de un día, desglosado por módulo (sparse: el front rellena ceros)."""

    day: str  # 'YYYY-MM-DD' en la TZ del bucket
    total: float
    by_module: dict[str, float]


class LlmRollup(BaseModel):
    kpis: LlmKpis
    by_source: list[LlmBySource]
    by_module: list[LlmByModule]
    by_model: list[LlmByModel]
    by_source_module: list[LlmSourceModuleCell]
    daily: list[LlmDailyPoint]
    # Keys de módulo presentes en el rango → series estables del área apilada en el front.
    modules: list[str]


class LlmCallRow(BaseModel):
    """Una llamada cruda para la auditoría (módulo derivado de `purpose`)."""

    id: int
    created_at: datetime
    purpose: str
    module: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cost_usd: float
    latency_ms: int
    status: str
    error_message: str | None = None
    inbox_id: int | None = None
    source_id: int | None = None
    source_name: str | None = None
    # Decisión de la fase: extracción {items, discarded, n, ...}; ruteo {slugs_in, chosen, ...}.
    metadata: dict[str, Any] | None = None


class LlmCallList(BaseModel):
    items: list[LlmCallRow]
    total: int


# ---- Observabilidad del pipeline (vistas /pipeline y /resumen) ----------------------------------
# Agregaciones de solo lectura sobre las tablas de observabilidad ya existentes (ingestion_runs,
# worker_runs, work_item_failures, mod_calendar_conflicts, inbox). El router `stats` las arma.


class StatsSourceRun(BaseModel):
    """Última corrida de ingesta de una fuente (subconjunto de `ingestion_runs`)."""

    started_at: datetime
    ended_at: datetime | None = None
    status: str  # running | ok | failed | aborted
    error_class: str | None = None
    error_message: str | None = None


class StatsSparkPoint(BaseModel):
    """Un punto del sparkline de insertados (corridas recientes de la fuente, viejo→nuevo)."""

    started_at: datetime
    inserted: int


class StatsSourceHealth(BaseModel):
    """Salud de una fuente: última corrida + agregados de por vida + sparkline."""

    source_id: int
    name: str
    type: str
    enabled: bool
    alias: str | None = None
    last_run: StatsSourceRun | None = None
    success_rate: float  # ok / terminadas (0..1); 0 si no hay corridas terminadas
    total_inserted: int
    total_filtered: int
    recent: list[StatsSparkPoint]


class StatsWorkerRun(BaseModel):
    """Última corrida de un worker del scheduler (subconjunto de `worker_runs`)."""

    started_at: datetime
    finished_at: datetime | None = None
    status: str  # running | ok | error
    stats: dict[str, Any]
    error: str | None = None


class StatsWorkerLatest(BaseModel):
    """Estado del último run de un job; `is_stale` = sigue 'running' pasados 30 min (colgado)."""

    job: str
    latest: StatsWorkerRun | None = None
    is_stale: bool


class StatsIngestionRun(BaseModel):
    """Una corrida de ingesta con el invariante posted = inserted+duplicates+errors+filtered."""

    id: str  # ingestion_runs.id es UUID
    source_id: int
    source_name: str | None = None
    trigger: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    error_class: str | None = None
    error_message: str | None = None
    expected: int  # inserted + duplicates + errors + filtered
    balanced: bool  # posted == expected


class StatsIngestionTotals(BaseModel):
    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    runs: int
    unbalanced: int  # nº de corridas con posted != expected


class StatsIngestion(BaseModel):
    runs: list[StatsIngestionRun]
    totals: StatsIngestionTotals


class StatsPipeline(BaseModel):
    sources: list[StatsSourceHealth]
    workers: list[StatsWorkerLatest]
    ingestion: StatsIngestion


class StatsReviewCounts(BaseModel):
    """Pendientes de revisión: dead-letter de workers + conflictos de calendar."""

    dead_letter: int
    calendar_conflicts: int
    total: int


class StatsOverview(BaseModel):
    review: StatsReviewCounts
    inbox_pending: int
    inbox_errors: int
    stale_workers: int


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
    # Cuenta vinculada (0018). Optional/default para back-compat de SELECTs que no la traen.
    account_id: int | None = None
    account_alias: str | None = None


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


class SourcePatch(BaseModel):
    """Edición parcial de una source. Usar `model_fields_set` para saber qué se setea."""

    account_id: int | None = None
    enabled: bool | None = None


class SocialAccountAdd(BaseModel):
    """Alta de una cuenta seguida en el allowlist de una source social.

    `handle` admite handle / nombre de página / URL completa: el backend lo normaliza
    al handle canónico (lowercase, sin `@` ni URL) con la MISMA función que usa el ingestor.
    """

    handle: str = Field(min_length=1)
    priority: bool = False


# ----- Auth / login (0018) -------------------------------------------------- #


class SignupRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class MeResponse(BaseModel):
    user_id: int
    email: str
    display_name: str | None = None
    auth_enforced: bool


# ----- Cuentas + credenciales (0018) --------------------------------------- #


class CredentialStatus(BaseModel):
    """Estado de un secreto SIN exponer el valor (solo máscara)."""

    secret_name: str
    configured: bool
    last4: str


class AccountCreate(BaseModel):
    alias: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    kind: Literal["email", "chat", "social"]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountPatch(BaseModel):
    alias: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class AccountRow(BaseModel):
    id: int
    user_id: int
    alias: str
    provider: str
    kind: str
    metadata: dict[str, Any]
    enabled: bool
    health_status: str
    last_health_check_at: datetime | None = None
    created_at: datetime
    secrets: list[CredentialStatus] = Field(default_factory=list)


class CredentialSet(BaseModel):
    """Entrada de una credencial. `value` es plaintext: se cifra ya y NUNCA se devuelve/loguea."""

    secret_name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class HealthCheckResponse(BaseModel):
    status: str
    detail: str
    checked_at: datetime


class OAuthStartResponse(BaseModel):
    authorization_url: str


# --- Procesamiento: módulos (toggle + cobertura) ---
BatchingPolicy = Literal["per_module", "grouped", "all"]


class ModuleRow(BaseModel):
    """Estado + cobertura de un módulo de extracción para /procesamiento."""

    slug: str
    label: str
    enabled: bool
    batching_policy: BatchingPolicy
    group_size: int
    processed: int  # inbox distintos con fila en module_extractions para el slug
    total: int  # inbox elegibles (clasificados + source.type ∈ consumes_kinds, media terminal)
    pending: int  # total - processed


class ModulePatch(BaseModel):
    """Edición parcial de un módulo. Usar `model_fields_set` para saber qué se setea."""

    enabled: bool | None = None
    batching_policy: BatchingPolicy | None = None
    group_size: int | None = Field(default=None, ge=1, le=100)


class ModuleList(BaseModel):
    items: list[ModuleRow]


# --- Procesamiento: corridas por lote (reprocess on-demand) ---
ProcessingStage = Literal["media", "ocr", "classify", "summarize", "extract"]
ProcessingOnly = Literal["unstored-attachments", "errored"]


class ProcessingRunRequest(BaseModel):
    """Selección + etapas de una corrida por lote (calca los filtros de `memex-reprocess`)."""

    stages: list[ProcessingStage] = Field(default_factory=list)
    source_id: int | None = None
    since: date | None = None  # inclusive (YYYY-MM-DD)
    until: date | None = None  # exclusivo (YYYY-MM-DD)
    limit: int | None = Field(default=None, ge=1, le=5000)
    only: ProcessingOnly | None = None
    force: bool = False


class ProcessingDryRun(BaseModel):
    """Previa sin escribir: cuántos objetivos caen bajo el filtro + una muestra de ids."""

    count: int
    sample_ids: list[int]
    stages: list[str]


class ProcessingRunStatus(BaseModel):
    """Respuesta inmediata de POST /processing/run (la corrida sigue en background)."""

    run_id: int | None = None
    status: str  # running | empty
    count: int = 0
    stages: list[str] = Field(default_factory=list)


class ProcessingRunRow(BaseModel):
    """Una corrida por lote (worker_runs con run_type='reprocess') para el polling de la UI."""

    id: int
    status: str  # running | ok | error
    stats: dict[str, Any]  # el dict de reprocess(): {targets, stages, results:{...}}
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    run_config: dict[str, Any]  # {stages, targets, force, filters}
    is_stale: bool


class ProcessingRunList(BaseModel):
    items: list[ProcessingRunRow]


# --- Procesamiento: control runtime del scheduler ---
class SchedulerSettingsPatch(BaseModel):
    """Cambio en runtime del daemon. `enabled_jobs` es CSV (mismo formato que el env)."""

    daemon_enabled: bool | None = None
    enabled_jobs: str | None = None


class SchedulerJobState(BaseModel):
    name: str
    default_interval: str  # ISO-8601 (PT15M, PT1H, ...)
    enabled: bool  # name ∈ enabled_jobs
    latest: StatsWorkerRun | None = None
    is_stale: bool = False


class SchedulerState(BaseModel):
    daemon_enabled: bool
    enabled_jobs: list[str]  # CSV parseado
    jobs: list[SchedulerJobState]
