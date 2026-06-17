from datetime import date, datetime, time
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


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
    media: list[MediaItem] = Field(default_factory=list)


class GatewayStateRequest(BaseModel):
    source_type: str
    #: Identidad de la cuenta del plugin (el email IMAP), reportada por el cliente local. Opcional:
    #: los plugins sin identidad la omiten.
    account_email: str | None = None


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


# ---- Geo / ubicación (gateway de pings GPS) -----------------------------------------------------
# La app móvil manda pings por POST /gateway/location/pings (append-only, SIN dedup). Coords en
# grados decimales; `captured_at` tz-aware (la columna es TIMESTAMPTZ). Los campos de movimiento
# son opcionales (solo exige posición + instante). `LocationFixRow` = read-back de la última.


class LocationPingIn(BaseModel):
    """Un ping GPS entrante. Obligatorio: posición + instante de captura (tz-aware)."""

    captured_at: datetime
    lat: float = Field(ge=-90.0, le=90.0)
    lng: float = Field(ge=-180.0, le=180.0)
    accuracy_m: float | None = Field(default=None, ge=0.0)
    altitude_m: float | None = None
    heading: float | None = Field(default=None, ge=0.0, le=360.0)
    speed_mps: float | None = Field(default=None, ge=0.0)
    source: Literal["device", "manual", "inferred"] = "device"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at debe incluir zona horaria (tz-aware)")
        return self


class LocationPingBatch(BaseModel):
    pings: list[LocationPingIn] = Field(default_factory=list)


class LocationIngestStats(BaseModel):
    inserted: int


class LocationFixRow(BaseModel):
    """Un ping almacenado (read-back de la última ubicación)."""

    id: int
    lat: float
    lng: float
    accuracy_m: float | None = None
    altitude_m: float | None = None
    heading: float | None = None
    speed_mps: float | None = None
    captured_at: datetime
    received_at: datetime
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    # `summaries.metadata` (source_id, n, truncated…): `n` es el tamaño real del lote al persistir
    # — el front lo usa para avisar "resumen del lote · n mensajes" en tier batch.
    metadata: dict[str, Any] | None = None


class ExtractionInfo(BaseModel):
    # `done` puede ser True con listas vacías: el cursor marca "procesado, sin datos relevantes".
    # Una clave por slug de módulo (la arma `read_extractions` iterando el registry).
    done: bool = False
    modules: list[str] = Field(default_factory=list)
    finance: list[dict[str, Any]] = Field(default_factory=list)
    calendar: list[dict[str, Any]] = Field(default_factory=list)
    hackathones: list[dict[str, Any]] = Field(default_factory=list)
    identidades: list[dict[str, Any]] = Field(default_factory=list)


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


class MediaListItem(MediaAssetInfo):
    """Un media_asset + contexto de su mensaje, para el monitor /ocr (GET /media)."""

    inbox_id: int
    subject: str | None = None
    occurred_at: datetime | None = None


class MediaList(BaseModel):
    items: list[MediaListItem] = Field(default_factory=list)
    next_cursor: int | None = None


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


class FeedbackStatusUpdate(BaseModel):
    """Cambio de estado de un feedback (gestión en Calidad y precisión)."""

    status: Literal["open", "reviewed", "dismissed"]


class SenderRelevance(BaseModel):
    """Relevancia agregada de un remitente (sistema de calidad). `relevance_pct` cuenta SOLO los
    mensajes que produjeron un hecho de dominio; `summarized_only` (se resumió pero sin hecho) e
    `inert` (ni hecho ni resumen) son buckets aparte para no lavar la señal."""

    sender_key: str
    sender_label: str
    messages: int
    relevant: int
    summarized_only: int
    inert: int
    marked: int
    # `email` no-null ⇒ remitente accionable (sender→tier es email-only en v1); `override_tier` = el
    # tier forzado activo ("muted") o null.
    email: str | None = None
    override_tier: str | None = None
    kind: str = "other"  # email | chat | social | other — para filtrar la vista por fuente
    relevance_pct: float | None = None
    last_at: datetime | None = None
    tier_mix: dict[str, int] = Field(default_factory=dict)
    volume_ratio: float | None = None


class SenderRelevanceList(BaseModel):
    items: list[SenderRelevance] = Field(default_factory=list)


class SenderTierRequest(BaseModel):
    """Dial de COSTO: fuerza el tier de los mensajes futuros de un remitente (batch/individual).

    «No procesar un remitente» ya NO es un tier: es una regla del gate (`POST /relevance/rules`
    kind=sender_email). El override de tier quedó solo como dial de costo sobre lo relevante.
    """

    sender_email: str
    tier: Literal["batch", "individual"] = "batch"
    reason: str | None = None


class SenderTierInfo(BaseModel):
    sender_email: str
    tier: str
    reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SenderTierList(BaseModel):
    items: list[SenderTierInfo] = Field(default_factory=list)


class RelevanceCandidate(BaseModel):
    """Candidato a (re)evaluar que armó un PROCEDIMIENTO determinista (sin accionar solo)."""

    procedure: str
    unit_type: str
    sender_key: str
    sender_label: str
    email: str | None = None
    messages: int
    relevant: int
    inert: int
    relevance_pct: float | None = None
    score: int
    status: str
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RelevanceCandidateList(BaseModel):
    items: list[RelevanceCandidate] = Field(default_factory=list)


class CandidateStatusRequest(BaseModel):
    sender_key: str
    status: Literal["open", "confirmed", "dismissed"]
    procedure: str | None = None


class ReevaluateRequest(BaseModel):
    sender_key: str
    procedure: str | None = None


class ReevaluateResponse(BaseModel):
    """Conteo de veredictos al re-evaluar la muestra de un candidato por el motor único."""

    messages: int
    relevant: int
    not_relevant: int
    insufficient: int


class RelevanceMarkRequest(BaseModel):
    """Marca manual de relevancia (override por-mensaje). `is_relevant=False` = ruido."""

    is_relevant: bool
    reason: str | None = None


class RelevanceMarkInfo(BaseModel):
    is_relevant: bool
    reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RelevanceVerdictInfo(BaseModel):
    """Veredicto del gate de relevancia para un mensaje (`relevance_verdicts`). Es la CONCLUSIÓN del
    gate —distinta del tier (dial de costo) y de la marca manual (override)—: relevant /
    not_relevant / insufficient, CÓMO se decidió (`method` rule/llm/manual), por qué (`reason`), con
    qué `mode` y, si fue por regla, qué regla compuesta (`rule_effect` + remitente + asunto).
    Solo en el detalle (GET /inbox/{id})."""

    verdict: str  # relevant | not_relevant | insufficient
    method: str  # rule | llm | manual
    reason: str | None = None
    mode: str | None = None
    model: str | None = None
    rule_id: int | None = None
    rule_effect: str | None = None  # block | allow (si method='rule')
    rule_sender_kind: str | None = None
    rule_sender_value: str | None = None
    rule_subject_pattern: str | None = None
    created_at: datetime | None = None


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
    # Árbol de traza jerárquica de la extracción (TraceNodeDto[] camelCase; ver core.trace).
    # Lista PLANA con parentId; null ⇒ mensaje sin árbol (procesado antes de la traza por lote).
    trace: list[dict[str, Any]] | None = None
    llm: LlmUsageInfo | None = None
    media: list[MediaAssetInfo] = Field(default_factory=list)
    # Feedback manual del usuario sobre este mensaje — solo en el detalle.
    feedback: FeedbackInfo | None = None
    # Marca manual de relevancia (override por-mensaje del sistema de calidad) — solo en el detalle.
    relevance: RelevanceMarkInfo | None = None
    # Veredicto del gate de relevancia (la conclusión: ¿se procesa?) — solo en el detalle.
    relevance_verdict: RelevanceVerdictInfo | None = None


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


class InboxWindow(BaseModel):
    """Lote de procesamiento de un mensaje (GET /inbox/{id}/window).

    `mode`: "summary" = co-miembros del resumen YA hecho (el lote que se procesó junto);
    "prospective" = la ventana que `plan_windows` armaría hoy sobre el work-set no-resumido
    (lo mismo que haría «Resumir su lote»); "none" = sin lote (blacklist / sin clasificar).
    `members` va en orden conversacional (occurred_at), incluye al propio mensaje.
    """

    mode: Literal["summary", "prospective", "none"]
    summary_id: int | None = None
    members: list[InboxRow] = Field(default_factory=list)


class StatsBySource(BaseModel):
    total: int
    pending: int
    errored: int


class InboxStats(BaseModel):
    sources: dict[int, StatsBySource]


# ---- Cobertura temporal (timeline de rangos cubiertos) ------------------------------------------
# Shape GENÉRICO lanes/ranges, sin nada específico de ingesta: hoy lo produce GET /inbox/coverage
# (rangos INGERIDOS por `occurred_at`); una futura vista de procesamiento puede producir el mismo
# shape con rangos procesados y reusar el componente de timeline del frontend tal cual.


class CoverageRange(BaseModel):
    """Tramo contiguo de días cubiertos (días separados por <= gap_days se funden en uno)."""

    start: date  # primer día del tramo (inclusive, en la tz pedida)
    end: date  # último día del tramo (inclusive)
    days: int  # días de calendario del tramo (end - start + 1)
    count: int  # items dentro del tramo


class CoverageSpan(BaseModel):
    """Tramo BARRIDO por la ingesta (reclamado por un fetch de rango), haya o no mensajes.

    Distingue "barrí y estaba vacío" de "nunca lo intenté". Sale de `ingest_swept_ranges`
    (bitácora append-only) + el avance del backfill_job vigente ([range_start, frontier)),
    con solapes/adyacencias fundidos.
    """

    start: date  # inclusive
    end: date  # inclusive
    days: int


class CoverageCursor(BaseModel):
    """Posición del cursor incremental de la fuente: hasta cuándo está al día.

    `at` es `source_checkpoints.updated_at` (última vez que el cursor avanzó o se confirmó al
    día); `day` su día en la tz pedida (posición en el eje); `summary` un resumen humano del
    cursor crudo ("" si no se pudo resumir).
    """

    at: datetime
    day: date
    summary: str


class CoverageLane(BaseModel):
    """Una pista del timeline (hoy: una fuente)."""

    id: int
    label: str  # sources.name
    kind: str  # email | chat | social | other (derivado de sources.type)
    enabled: bool
    total: int
    first_day: date | None
    last_day: date | None
    ranges: list[CoverageRange]
    swept: list[CoverageSpan]
    cursor: CoverageCursor | None = None


class CoverageOut(BaseModel):
    lanes: list[CoverageLane]
    domain_min: date | None  # extremos entre lanes (items Y barridos); None si no hay nada
    domain_max: date | None
    tz: str
    gap_days: int


class FinanceTransactionRow(BaseModel):
    """Una transacción CONSOLIDADA (fila de `mod_finance_consolidated` — la vista deduplicada que
    lee el dashboard).

    `amount` cruza como `float` (la DB es NUMERIC(14,2)) siguiendo la convención del repo para
    dinero en respuestas (cf. `cost_usd`). `occurred_at` es el mejor instante conocido del cobro;
    `occurred_at_precision` ('datetime'|'date'|'inferred') dice si la hora es del cobro, solo la
    fecha, o inferida de la recepción del mensaje. `direction` es 'ingreso' | 'egreso'.

    `evidence` y `source_inbox_ids` NO viven en `mod_finance_consolidated`: se recuperan en el query
    de las crudas (`mod_finance_transactions`) — `evidence` de la transacción ganadora
    (`winner_transaction_id`) y `source_inbox_ids` como unión de los mensajes de todas las crudas
    enlazadas (vía `mod_finance_transaction_links`). Alimentan, en el dashboard, la evidencia del
    movimiento y el link al correo de origen (trazabilidad). Degradan a '' / [] si faltan.
    """

    id: int
    direction: str
    amount: float
    currency: str
    category: str
    counterparty: str
    place: str
    occurred_at: datetime
    occurred_at_precision: str
    description: str
    evidence: str
    source_inbox_ids: list[int]
    created_at: datetime
    # Lugar resuelto del catálogo (`geo_places` vía `place_id`), como en /calendar/events:
    # NULL si el pago no tiene lugar asociado (seam GPS o `memex finance set-place`).
    place_name: str | None = None
    place_address: str | None = None


class FinanceTransactionList(BaseModel):
    items: list[FinanceTransactionRow]
    next_cursor: int | None = None


# ---- Módulo bienestar (registrador determinista) ------------------------------------------------
# Registros: solo LECTURA (la escritura va por CLI/agente). Hábitos: lectura + alta/baja (el usuario
# los gestiona desde el dashboard). JSONB (detail/metadata) → dict; TIMESTAMPTZ → ISO. La adherencia
# se calcula en lectura.


class BienestarRegistroRow(BaseModel):
    """Un registro de bienestar (fila de `mod_bienestar_registros`). `event_id` correlaciona hechos
    del mismo mensaje del agente (NULL = suelto)."""

    id: int
    category: str
    activity: str
    occurred_at: datetime
    occurred_at_precision: str
    description: str
    detail: dict[str, Any]
    metadata: dict[str, Any]
    event_id: str | None
    created_at: datetime


class BienestarRegistroList(BaseModel):
    items: list[BienestarRegistroRow]


class BienestarSummary(BaseModel):
    total: int
    by_category: dict[str, int]
    by_activity: dict[str, int]
    since: datetime | None = None
    until: datetime | None = None


class BienestarDailyRow(BaseModel):
    day: str
    total: int
    by_category: dict[str, int]


class BienestarDaily(BaseModel):
    days: list[BienestarDailyRow]


class BienestarHabitPoint(BaseModel):
    period: str
    count: int
    met: bool


class BienestarHabitAdherence(BaseModel):
    """Un hábito + su adherencia derivada (racha con gracia del período en curso + historia)."""

    habit: dict[str, Any]
    cadence: str
    target_count: int
    current: int
    met_current: bool
    streak: int
    history: list[BienestarHabitPoint]


class BienestarHabitList(BaseModel):
    items: list[BienestarHabitAdherence]


class BienestarHabitRow(BaseModel):
    """Un hábito (fila de `mod_bienestar_habits`); lo que devuelve la creación."""

    id: int
    name: str
    activity: str
    category: str | None
    cadence: str
    target_count: int
    active: bool
    created_at: datetime


class BienestarHabitCreate(BaseModel):
    """Alta de un hábito desde el dashboard. Necesita `activity` (clave de match) o `category` — lo
    valida el dominio (`add_habit`), que responde 422 si falta."""

    name: str = Field(min_length=1)
    cadence: Literal["daily", "weekly"]
    target_count: int = Field(default=1, ge=1)
    activity: str = ""
    category: str | None = None


# ---- Módulo hackathones (extractor puro) --------------------------------------------------------
# El dashboard lee `mod_hackathones_events` de SOLO LECTURA (GET). Las fechas pueden ser NULL (un
# anuncio suele traer solo el deadline de inscripción); el front decide cómo mostrarlas.


class HackathonRow(BaseModel):
    """Un hackatón extraído (fila de `mod_hackathones_events`).

    Espeja `memex.modules.hackathones.schema.HackathonItem` más las columnas de la tabla. Las tres
    fechas son nullable: `name` es el único campo de dominio obligatorio.
    """

    id: int
    name: str
    starts_on: date | None
    ends_on: date | None
    registration_deadline: date | None
    modality: str
    location: str
    url: str
    organizer: str
    technologies: str
    prizes: str
    requirements: str
    description: str
    evidence: str
    source_inbox_ids: list[int]
    created_at: datetime


class HackathonList(BaseModel):
    items: list[HackathonRow]
    next_cursor: int | None = None


# ---- Módulo calendar (dominio bidireccional) ----------------------------------------------------
# El dashboard lee la capa CONSOLIDADA (`mod_calendar_consolidated`) + sus miembros crudos
# (`event_links` → `mod_calendar_events`), los pares de dedup, los conflictos, las corridas de sync
# y las cuentas de proveedor. Todo de SOLO LECTURA (GET): la UI de calendario hoy no muta nada.
# Convenciones: NUMERIC → float (igual que finance), TIME → "HH:MM:SS" (el front recorta a HH:MM),
# DATE → "YYYY-MM-DD", TIMESTAMPTZ → ISO. NUNCA se expone el token del proveedor (ADR-001).


class CalendarRawMemberRow(BaseModel):
    """Un evento crudo (`mod_calendar_events`) que compone un consolidado vía `event_links`."""

    id: int
    origin: str
    provider: str | None
    source_inbox_ids: list[int]
    evidence: str
    processing_outcome: str
    is_winner: bool


class CalendarConsolidatedRow(BaseModel):
    """Un evento consolidado (`mod_calendar_consolidated`) + sus miembros crudos.

    `protected`/`priority_rank` salen del miembro GANADOR (`winner_event_id`, donde vive la
    prioridad). `origins` son los orígenes distintos de los miembros (para los puntitos de color).
    `place_name`/`place_address` = lugar canónico del catálogo geo (FK `place_id`); None si el
    `location` no se resolvió todavía o es virtual (link de Meet, etc.).
    """

    id: int
    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    description: str
    place_name: str | None
    place_address: str | None
    member_count: int
    origins: list[str]
    protected: bool
    priority_rank: int
    members: list[CalendarRawMemberRow]


class CalendarEventList(BaseModel):
    items: list[CalendarConsolidatedRow]
    next_cursor: int | None = None


class CalendarEventLiteRow(BaseModel):
    """Una punta liviana de un par de dedup (fila de `mod_calendar_events`)."""

    id: int
    title: str
    starts_on: date
    start_time: time | None
    location: str
    origin: str
    provider: str | None
    source_inbox_ids: list[int]


class CalendarDedupDecisionRow(BaseModel):
    """Un par candidato de dedup (`mod_calendar_dedup_candidates`) + sus dos eventos.

    `score`/`confidence` cruzan como float (DB NUMERIC). `decided_by` es 'llm'|'manual'|None
    (FASE 2 LLM o decisión manual); None ⇒ aún `candidate` sin resolver.
    """

    id: int
    a: CalendarEventLiteRow
    b: CalendarEventLiteRow
    reason: str
    score: float | None
    status: str
    decided_by: str | None
    confidence: float | None
    rationale: str | None
    decided_at: datetime | None


class CalendarDedupList(BaseModel):
    items: list[CalendarDedupDecisionRow]
    next_cursor: int | None = None


class CalendarConsolidatedLiteRow(BaseModel):
    """Una punta liviana de un conflicto (consolidado + prioridad de su ganador)."""

    id: int
    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    priority_rank: int
    protected: bool


class CalendarConflictRow(BaseModel):
    """Un conflicto (o GRUPO de conflictos de un mismo par de series recurrentes).

    Dos consolidados DISTINTOS de alta importancia que chocan en horario. Las instancias de un
    mismo par de series recurrentes se agrupan en un item: `a`/`b` son el representante (la
    ocurrencia más próxima), `instance_count` cuántas veces se repite, `first_on`/`last_on` el
    rango, `recurring` = se repite (>1 instancia).
    """

    id: int
    a: CalendarConsolidatedLiteRow
    b: CalendarConsolidatedLiteRow
    reason: str
    status: str
    created_at: datetime
    instance_count: int = 1
    recurring: bool = False
    first_on: date
    last_on: date


class CalendarConflictList(BaseModel):
    items: list[CalendarConflictRow]
    next_cursor: int | None = None


class CalendarSyncRunRow(BaseModel):
    """Una corrida de sync con un proveedor (`mod_calendar_sync_runs`)."""

    id: int
    account: str
    direction: str
    pulled: int
    created: int
    modified: int
    deleted: int
    unchanged: int
    dedup_pairs: int
    errors: int
    status: str
    started_at: datetime
    finished_at: datetime | None


class CalendarSyncRunList(BaseModel):
    items: list[CalendarSyncRunRow]
    next_cursor: int | None = None


class CalendarProviderAccountRow(BaseModel):
    """Una cuenta de proveedor de calendario (`mod_calendar_provider_accounts`).

    NO expone el token: `token_path_env` es el NOMBRE de la env var (ADR-001) y `sync_token_present`
    solo dice si hay cursor delta (el front deriva 'delta' / 'full-resync' / 'never').
    """

    id: int
    provider: str
    account_label: str
    calendar_id: str
    last_sync_at: datetime | None
    token_path_env: str
    enabled: bool
    write_back: bool
    sync_token_present: bool


class CalendarProviderAccountList(BaseModel):
    items: list[CalendarProviderAccountRow]


class CalendarAccountHealth(BaseModel):
    """Salud de sync de UNA cuenta de proveedor, en términos operables.

    `cursor_state`: 'incremental' (cursor delta vivo: la próxima bajada trae solo cambios) |
    'full_resync_pendiente' (sin cursor pero ya sincronizó: la próxima será completa) |
    'sin_primera_sync' (nunca bajó nada).
    """

    account_id: int
    provider: str
    account_label: str
    enabled: bool
    write_back: bool
    cursor_state: str
    last_pull_at: datetime | None
    last_pull_status: str | None
    last_pull_age_hours: float | None
    last_push_at: datetime | None
    last_push_status: str | None


class CalendarSyncHealth(BaseModel):
    """¿La sincronización de calendario está funcionando? (UI y CLI comparten esta fuente).

    `overall`: 'ok' (última bajada <24 h y sin error) | 'desactualizado' | 'error' (la última
    bajada falló) | 'nunca' (jamás sincronizó) | 'sin_cuentas'. `auto_sync_active` = el daemon
    del scheduler está prendido Y el job `calendar` habilitado (si no, los datos solo se
    actualizan a mano)."""

    overall: str
    auto_sync_active: bool
    daemon_enabled: bool
    calendar_job_enabled: bool
    last_cycle_at: datetime | None
    accounts: list[CalendarAccountHealth]


class CalendarSyncNowResponse(BaseModel):
    """Resultado de POST /calendar/accounts/{id}/sync: pull + consolidación (sin LLM ni push)."""

    pulled: int
    created: int
    modified: int
    deleted: int
    unchanged: int
    dedup_pairs: int
    errors: int
    groups: int
    orphans: int
    status: str


class CalendarSettings(BaseModel):
    """Perillas del módulo calendar (`module_settings.config`).

    `llm_on_past_events`: ¿el dedup FASE 2 y el merge (pasos que GASTAN LLM) procesan eventos ya
    vencidos? Default False — no gastar en lo que ya pasó; lo salteado se retoma al prenderla."""

    llm_on_past_events: bool


class CalendarSettingsPatch(BaseModel):
    llm_on_past_events: bool


# ---- Módulo identidades (tablas `mod_identidades_*`) --------------------------------------------


class IdentityIdentifierRow(BaseModel):
    """Un identificador por-fuente de una identidad (`mod_identidades_identifiers`)."""

    id: int
    platform: str
    kind: str
    value: str
    is_primary: bool = False
    source: str


class IdentitySiteRow(BaseModel):
    """Una sede de una organización (`mod_identidades_sites`)."""

    id: int
    label: str = ""
    address: str = ""
    country: str | None = None


class IdentityRow(BaseModel):
    """Una identidad del directorio unificado (`mod_identidades`), persona u organización."""

    id: int
    kind: str
    display_name: str
    aliases: list[str] = []
    interest: bool = False
    source: str
    notes: str = ""
    given_name: str | None = None
    family_name: str | None = None
    birthday: date | None = None
    photo_url: str | None = None
    deleted: bool = False
    #: Pertenencia («sub»): de qué identidad cuelga esta (None = sin padre). `parent_name` se
    #: rellena en lista/detalle (JOIN); `mention_count` es el nº de menciones resueltas a esta.
    parent_id: int | None = None
    parent_name: str | None = None
    mention_count: int = 0
    created_at: datetime
    updated_at: datetime


class IdentityList(BaseModel):
    items: list[IdentityRow]
    next_cursor: int | None = None


class IdentityMentionRow(BaseModel):
    """Una mención cruda extraída (`mod_identidades_mentions`), con su resolución determinista."""

    id: int
    source_inbox_ids: list[int] = []
    evidence: str = ""
    mentioned_name: str
    mentioned_kind: str
    email: str | None = None
    handle: str | None = None
    org_hint: str | None = None
    role_hint: str | None = None
    confidence: float | None = None
    resolved_kind: str | None = None
    resolved_identity_id: int | None = None
    resolution_method: str | None = None
    created_at: datetime


class IdentityMentionList(BaseModel):
    items: list[IdentityMentionRow]
    next_cursor: int | None = None


class IdentityAffiliationRow(BaseModel):
    """La contraparte de una afiliación persona↔org (la otra identidad + el rol)."""

    id: int
    kind: str
    display_name: str
    role: str | None = None


class IdentityChildRow(BaseModel):
    """Una sub-identidad que cuelga de esta (su `parent_identity_id` apunta acá)."""

    id: int
    kind: str
    display_name: str


class IdentityDetail(BaseModel):
    """Una identidad + sus identificadores, sedes, afiliaciones, menciones y sub-identidades."""

    identity: IdentityRow
    identifiers: list[IdentityIdentifierRow] = []
    sites: list[IdentitySiteRow] = []
    affiliations: list[IdentityAffiliationRow] = []
    mentions: list[IdentityMentionRow] = []
    #: Las identidades que pertenecen a esta (sus «partes»: programas, productos, filiales, …).
    children: list[IdentityChildRow] = []


class IdentityProviderAccountRow(BaseModel):
    """Una cuenta de proveedor de contactos (`mod_identidades_provider_accounts`).

    NO expone el token: `account_id` apunta a la cuenta del dashboard cuyo vault lo tiene;
    `sync_token_present` solo dice si hay cursor delta."""

    id: int
    provider: str
    account_label: str
    account_id: int | None = None
    enabled: bool
    last_sync_at: datetime | None = None
    sync_token_present: bool


class IdentityProviderAccountList(BaseModel):
    items: list[IdentityProviderAccountRow]


class IdentitySyncRunRow(BaseModel):
    """Una corrida de sync de contactos (`mod_identidades_sync_runs`)."""

    id: int
    provider_account_id: int | None = None
    pulled: int
    created: int
    modified: int
    deleted: int
    unchanged: int
    errors: int
    status: str
    started_at: datetime
    finished_at: datetime | None = None


class IdentitySyncRunList(BaseModel):
    items: list[IdentitySyncRunRow]
    next_cursor: int | None = None


class IdentityCreate(BaseModel):
    """Alta manual de una identidad (`kind` se valida en el router)."""

    kind: str = "organizacion"
    display_name: str
    aliases: list[str] = []
    interest: bool = True
    notes: str = ""
    given_name: str | None = None
    family_name: str | None = None
    birthday: date | None = None


class IdentityUpdate(BaseModel):
    display_name: str | None = None
    kind: str | None = None
    interest: bool | None = None
    notes: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    birthday: date | None = None
    aliases: list[str] | None = None
    #: Pertenencia: setear el padre (int) o limpiarlo (null). Se distingue "no enviado" de "null"
    #: con `model_dump(exclude_unset=True)` en el router (None explícito = quitar el padre).
    parent_id: int | None = None


class IdentityIdentifierCreate(BaseModel):
    platform: str
    kind: str
    value: str
    is_primary: bool = False


class IdentitySiteCreate(BaseModel):
    label: str = ""
    address: str = ""
    country: str | None = None


class IdentityAffiliateCreate(BaseModel):
    org_id: int
    role: str | None = None


class IdentityMergeCandidateRow(BaseModel):
    """Un par candidato a fusionar, con el nombre de cada lado."""

    id: int
    identity_a_id: int
    identity_b_id: int
    a_name: str
    b_name: str
    kind: str
    reason: str
    score: float | None = None
    status: str


class IdentityMergeCandidateList(BaseModel):
    items: list[IdentityMergeCandidateRow]


class IdentityMergeRequest(BaseModel):
    survivor_id: int
    absorbed_id: int


class IdentitySyncRequest(BaseModel):
    account_id: int
    full: bool = False


class IdentitySyncResult(BaseModel):
    pulled: int
    created: int
    modified: int
    deleted: int
    unchanged: int
    errors: int


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


class LlmCallDetail(LlmCallRow):
    """Detalle de UNA llamada (auditoría): una fila del list endpoint MÁS el texto crudo del LLM
    (`response_text`), que el list omite para no inflarse. NULL = no capturado."""

    response_text: str | None = None


class LlmCallList(BaseModel):
    items: list[LlmCallRow]
    total: int


# ---- Métricas de costo Apify (tabla apify_runs) -------------------------------------------------
# Espejo acotado del rollup LLM: acá la unidad es el RUN DE ACTOR (una corrida de scraping de UNA
# cuenta seguida) y el costo viene de Apify (usageTotalUsd), no de una tabla de precios local.


class ApifyKpis(BaseModel):
    """KPIs del rango: gasto Apify, corridas de actor y volumen scrapeado."""

    cost_usd: float
    runs: int
    items_scraped: int
    items_kept: int
    errors: int  # runs con status != 'ok' (error + timeout) — pudieron cobrar igual
    accounts: int  # cuentas seguidas distintas con actividad en el rango
    prev_cost_usd: float | None = None
    prev_runs: int | None = None


class ApifyBySource(BaseModel):
    """Gasto por fuente. `source_id` None = fuente borrada (el gasto histórico se conserva)."""

    source_id: int | None
    source_name: str
    runs: int
    items_scraped: int
    cost_usd: float


class ApifyByAccount(BaseModel):
    """Gasto por cuenta seguida — la unidad real de scraping (un run de actor por cuenta)."""

    platform: str
    account: str
    runs: int
    items_scraped: int
    cost_usd: float


class ApifyByPlatform(BaseModel):
    platform: str
    runs: int
    items_scraped: int
    cost_usd: float


class ApifyDailyPoint(BaseModel):
    """Gasto de un día, desglosado por plataforma (sparse: el front rellena ceros)."""

    day: str  # 'YYYY-MM-DD' en la TZ del bucket
    total: float
    by_platform: dict[str, float]


class ApifyRollup(BaseModel):
    kpis: ApifyKpis
    by_source: list[ApifyBySource]
    by_account: list[ApifyByAccount]
    by_platform: list[ApifyByPlatform]
    daily: list[ApifyDailyPoint]
    # Plataformas presentes en el rango → series estables del área apilada en el front.
    platforms: list[str]


class ApifyRunRow(BaseModel):
    """Un run de actor crudo para la auditoría. `cost_usd` NULL = Apify aún no lo asentó."""

    id: int
    created_at: datetime
    platform: str
    account: str
    actor_id: str
    apify_run_id: str | None = None
    status: str
    items_scraped: int
    items_kept: int
    cost_usd: float | None = None
    charged_events: dict[str, int] | None = None
    source_id: int | None = None
    source_name: str | None = None
    ingestion_run_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ApifyRunList(BaseModel):
    items: list[ApifyRunRow]
    total: int


# ---- Log events (vista /logs) -------------------------------------------------------------------
# Filas crudas de `log_events` (el sink de structlog, migración 0020) + agregaciones para el panel
# de métricas. `fields` es el resto de kwargs estructurados del evento; `exception` el traceback
# formateado cuando lo hubo. Las llamadas LLM aparecen acá como event='llm.call'.


class LogEventRow(BaseModel):
    """Un evento persistido por el sink (una línea de log consultable)."""

    id: int
    ts: datetime
    level: str
    event: str
    logger: str | None = None
    user_id: int | None = None
    request_id: str | None = None
    run_id: str | None = None
    source_id: int | None = None
    inbox_id: int | None = None
    exception: str | None = None
    fields: dict[str, Any]


class LogEventList(BaseModel):
    items: list[LogEventRow]
    total: int


class LogLevelCount(BaseModel):
    level: str
    count: int


class LogEventCount(BaseModel):
    event: str
    count: int


class LogLoggerCount(BaseModel):
    logger: str
    count: int


class LogHistogramPoint(BaseModel):
    """Un bucket temporal del histograma (granularidad según el rango: minuto/hora/día)."""

    bucket: datetime
    total: int
    errors: int


class LogLatency(BaseModel):
    """Percentiles de `fields->>'duration_ms'` (solo eventos que lo llevan; null si ninguno)."""

    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


class LogStats(BaseModel):
    total: int
    errors: int
    error_rate: float
    by_level: list[LogLevelCount]
    by_event: list[LogEventCount]
    by_logger: list[LogLoggerCount]
    histogram: list[LogHistogramPoint]
    latency: LogLatency
    # Eventos descartados por overflow de la cola del sink (no silent cap): el front lo muestra.
    sink_dropped: int


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
    account_email: str | None = None
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
    api_cost_usd: float | None = None  # gasto Apify de la corrida (None = sin API paga)
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
    api_cost_usd: float = 0.0  # gasto Apify sumado de las corridas listadas


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


class StatsAlert(BaseModel):
    """Alerta derivada de la observabilidad REAL (no mock): ingesta con última corrida fallida,
    worker colgado/en error o backlog de revisión. Lista vacía = todo en orden."""

    id: str
    severity: Literal["critica", "alta", "info"]
    kind: Literal["saldo", "worker-stale", "run-failed", "source-stale", "review"]
    title: str
    detail: str
    at: datetime
    read: bool = False
    deep_link: str


class ReviewDeadLetterItem(BaseModel):
    """Mensaje en 'pendiente de revisión' (dead-letter) con contexto del inbox para la cola."""

    id: int
    stage: str
    inbox_id: int
    attempts: int
    last_error: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    #: Preview legible del mensaje original (asunto/cuerpo), para no tener que abrir el inbox.
    preview: str


class ReviewActionResult(BaseModel):
    ok: bool


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
    # Identidad real de la cuenta/buzón (el email): de `accounts.metadata.email` (server-side) o de
    # `config.account_email` (cliente local). Para rotular de qué correo es la fuente.
    account_email: str | None = None
    # Agenda de ingesta (0025): intervalo ISO-8601 (PT1H, P1D…) o None = no se agenda.
    fetch_schedule: str | None = None
    # De dónde resuelve el token de Apify esta fuente (solo redes): "vault" = secreto cifrado de la
    # cuenta vinculada (pisa al env), "env" = variable del contenedor (Doppler), "missing" = no
    # resuelve (el fetch fallará). None = tipo sin token reportable (correo/telegram/etc.).
    token_source: Literal["vault", "env", "missing"] | None = None
    # Modos del fetch a demanda que el ingestor de este tipo HONRA (la UI habilita opciones por
    # esto, no por type hardcodeado) + avisos por modo (server-driven, p. ej. rango de Instagram).
    fetch_modes: list[str] = Field(default_factory=list)
    mode_caveats: dict[str, str] = Field(default_factory=dict)
    # Categoría conceptual del tipo (server-driven: la UI agrupa fuentes por medio leyendo esto,
    # nunca hardcodeando types). None = tipo sin kind registrado (calendar/gateway/dummy).
    kind: Literal["email", "chat", "social"] | None = None


class CheckpointBody(BaseModel):
    cursor: dict[str, Any]


class FetchResponse(BaseModel):
    """Resultado de una corrida de fetch a demanda (`POST /sources/{id}/fetch`).

    En dry-run los contadores son lo que PASARÍA (sin escribir): `inserted` = nuevos,
    `duplicates` = ya existentes ignorados, `filtered` = descartados por filter_rules.
    `posted` = total escaneado que cruzó el wire. `api_cost_usd` = costo real de API
    externa paga (Apify) de ESTA corrida — también viene en dry-run, que gasta igual;
    None para fuentes sin costo por corrida (correo/telegram) o si Apify aún no lo asentó.
    """

    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    dry_run: bool
    ms_elapsed: int
    api_cost_usd: float | None = None


class SourcePatch(BaseModel):
    """Edición parcial de una source. Usar `model_fields_set` para saber qué se setea.

    `fetch_schedule`: intervalo ISO-8601 (PT15M, PT1H, P1D…) para la ingesta agendada, o `null`
    explícito para quitar el agendado. Se valida con `parse_duration` en el router (422 si malo).
    Mandarlo ausente (no en el JSON) lo deja como está; mandarlo `null` lo limpia.
    """

    account_id: int | None = None
    enabled: bool | None = None
    fetch_schedule: str | None = None
    # Editar la config general (server/port/folders IMAP, chats Telegram…) o el nombre de la fuente,
    # para corregir una source mal configurada sin recrearla (y perder su cursor). `null`/ausente no
    # la toca. La allowlist social se edita por sus endpoints dedicados (/social/accounts).
    name: str | None = None
    config: dict[str, Any] | None = None


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
    #: Origen de la credencial cuando está configurada: "vault" (cifrada en DB) o "env" (variable
    #: de entorno del contenedor). "" si falta. Evita marcar "FALTA" lo que funciona vía env (H-11).
    source: str = ""


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
ProcessingStage = Literal["media", "ocr", "classify", "relevance", "extract"]
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


# --- Procesamiento: lote por ventanas (0056) ---
class ProcessingLotConfig(ProcessingRunRequest):
    """Alta/reconfiguración del lote: los mismos filtros de una corrida + tamaño de ventana.

    `window_size` opcional: sin él se resuelve por medio (min de los defaults de los kinds
    presentes en el snapshot).
    """

    window_size: int | None = Field(default=None, ge=1, le=5000)


class ProcessingLotAdvance(BaseModel):
    """Override del tamaño de ventana para ESTE avance; queda como nuevo default del lote."""

    window_size: int | None = Field(default=None, ge=1, le=5000)


class ProcessingLotWindow(BaseModel):
    """Una ventana ejecutada (item del history). `end_idx` exclusivo; índices 0-based."""

    start_idx: int
    end_idx: int
    n: int
    results: dict[str, Any]  # resultado por etapa de reprocess() (mismos slots que una corrida)
    errors: int  # suma de los `errors` por-mensaje de las etapas
    cost_usd: float
    ms_elapsed: int
    at: datetime


class ProcessingLotState(BaseModel):
    """Estado del lote para la UI: progreso (frontier/total), gasto y defaults por medio."""

    stages: list[str]
    filters: dict[str, Any]  # eco de la creación (source_id/since/until/limit/only)
    force: bool
    total: int
    frontier: int  # mensajes ya procesados (índice dentro del snapshot)
    window_size: int
    status: str  # active | done
    spent_usd: float  # suma de cost_usd del history
    busy: bool  # hay una corrida reprocess en curso (deshabilita avanzar)
    defaults: dict[str, int]  # tamaño de ventana por medio (email/chat/social)
    history: list[ProcessingLotWindow]
    created_at: datetime


class ProcessingLotAdvanceStatus(BaseModel):
    """Respuesta inmediata de POST /processing/lot/advance[-rest] (corre en background)."""

    run_id: int | None = None
    status: str  # running | done (no quedaba nada)
    window: dict[str, int] | None = None  # {start_idx, end_idx} de la próxima ventana (modo 1)


class WindowDefaultsPatch(BaseModel):
    """Edición de los defaults de ventana por medio; solo los kinds enviados se tocan."""

    sizes: dict[str, int]


class WindowDefaults(BaseModel):
    sizes: dict[str, int]


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


# --- Ingesta: control runtime del daemon de ingesta agendada (0025) ---
class IngestionRunRow(BaseModel):
    """Una corrida de ingesta (`ingestion_runs`) para el historial de /carga + deep-link a /logs.

    `id` (UUID como string) es la clave del link `/logs?run_id=<id>`. `trigger` es el ORIGEN
    (manual/daemon/backfill/agent/cli). Los contadores son `NOT NULL DEFAULT 0`; los
    timestamps/error son nullable mientras la corrida está 'running'.
    """

    id: str
    source_id: int
    trigger: str
    status: str  # running | ok | failed | aborted
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    error_class: str | None = None
    error_message: str | None = None
    # Gasto de API externa paga (Apify) de la corrida — agregado de sus apify_runs (0055).
    api_cost_usd: float | None = None
    is_stale: bool


class IngestionRunList(BaseModel):
    items: list[IngestionRunRow]


class IngestScheduleSource(BaseModel):
    """Una fuente en el panel de ingesta agendada: su schedule + su última corrida.

    `config` viaja para que el front derive icono/etiqueta de proveedor (`sourceMeta` distingue
    Gmail/Outlook por `config.host/server`) — coherencia visual con el resto del dashboard.
    """

    source_id: int
    name: str
    type: str
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)
    fetch_schedule: str | None = None  # ISO-8601 o None = no agendada
    latest: IngestionRunRow | None = None


class IngestSchedulerState(BaseModel):
    daemon_enabled: bool
    sources: list[IngestScheduleSource]


class IngestSchedulerPatch(BaseModel):
    """Cambio en runtime del master toggle del daemon de ingesta. El daemon lo relee cada tick."""

    daemon_enabled: bool | None = None


# --- Backfill segmentado (importación masiva por ventanas) ----------------------------------------
# Importa `[range_start, range_end]` de una fuente de correo en ventanas que avanzan hacia adelante.
# `range_end` viaja INCLUSIVO en la API (la UI elige "hasta"); el router suma 1 día para la DB →
# exclusivo, como el `until` del fetch (IMAP `BEFORE` es exclusivo).
BackfillWindowUnit = Literal["day", "week", "month"]


class BackfillConfig(BaseModel):
    """Alta/reconfiguración de un backfill por fuente. Resetea la frontera al inicio del rango."""

    range_start: date  # fecha1, inclusiva
    range_end: date  # fecha2, INCLUSIVA (el router la convierte a exclusiva para la DB)
    window_unit: BackfillWindowUnit = "month"
    window_count: int = Field(default=1, ge=1, le=365)
    per_window_limit: int = Field(default=2000, ge=1, le=10000)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.range_end < self.range_start:
            raise ValueError("range_end debe ser >= range_start")
        return self


class BackfillAdvanceOverride(BaseModel):
    """Override del tamaño de ventana para ESTE avance; se guarda como nuevo default."""

    window_unit: BackfillWindowUnit | None = None
    window_count: int | None = Field(default=None, ge=1, le=365)


class BackfillWindowResult(BaseModel):
    """Una ventana ejecutada (item del history); `end` exclusivo, `cap_hit` = quizá truncado."""

    start: date
    end: date
    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    cap_hit: bool
    ms_elapsed: int
    at: datetime


class BackfillState(BaseModel):
    """Estado completo del backfill para restaurar la UI. `range_end` vuelve INCLUSIVO."""

    source_id: int
    range_start: date
    range_end: date  # inclusiva (como la eligió el usuario)
    frontier: date
    window_unit: BackfillWindowUnit
    window_count: int
    per_window_limit: int
    status: str  # active | done
    progress_pct: float
    history: list[BackfillWindowResult] = Field(default_factory=list)


class BackfillAdvanceResponse(BaseModel):
    """Resultado de avanzar: la ventana corrida (None si ya completo) + el estado resultante."""

    window: BackfillWindowResult | None
    state: BackfillState
    dry_run: bool


# ---- Grafo de relaciones (vértices + aristas) ---------------------------------------------------
# El front lee el grafo de SOLO LECTURA (GET /graph). Un vértice se direcciona por (slug, id); inbox
# NO es vértice (es procedencia). Las aristas llevan su `producer` (quién las formó) y DOS EJES:
# `provenance` (extracted/inferred) por `verdict` (confirmed/rejected/ambiguous) + `label`
# y `relation` (justificación corta).


class GraphNode(BaseModel):
    """Un vértice del grafo, proyectado uniformemente desde su tabla de dominio (`mod_*`)."""

    slug: str
    id: int
    label: str
    kind: str
    source_inbox_ids: list[int] = []


class GraphEdge(BaseModel):
    """Una arista del grafo: referencia `src`→`dst` con su productor y sus dos ejes. `provenance`
    (cómo lo sabemos) por `verdict` (la decisión) derivan `label` (EXTRACTED/INFERRED/...).
    `relation` es la justificación corta; `dirty` el flag de groundwork incremental. `confidence`
    cruza como `float` (la DB es NUMERIC). `source_inbox_ids`: TODOS los mensajes que generaron la
    co-ocurrencia (`relation_edge_sources`); vacío para otros productores."""

    id: int
    src_slug: str
    src_id: int
    dst_slug: str
    dst_id: int
    relation_type: str
    producer: str
    provenance: str
    verdict: str
    label: str
    relation: str
    dirty: bool
    confidence: float | None
    evidence: str
    source_inbox_ids: list[int] = []


class GraphResponse(BaseModel):
    """GET /graph. `inbox_kinds`: medio (email|chat|social) por id de inbox REFERENCIADO por algún
    nodo — el front etiqueta «correo/chat/social #N» sin otra llamada; un id sin entrada (fila
    borrada o tipo sin SourceKind) cae a «mensaje #N»."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    inbox_kinds: dict[int, str] = {}


class GraphReconcileResult(BaseModel):
    """Resumen del mantenimiento del grafo (POST /graph/reconcile): reconciliación de reales stale
    + poda de huérfanas."""

    stale_afiliacion: int = 0
    stale_pertenencia: int = 0
    stale_contraparte: int = 0
    orphans_pruned: int = 0


class GraphClusterResult(BaseModel):
    """Resumen de POST /graph/cluster (detección de cúmulos + reconciliación, sin LLM)."""

    detected: int
    matched_same: int
    matched_drift: int
    new_candidates: int
    memo_skipped: int
    deleted: int
    dissolved: int


class GraphClusterValidateResult(BaseModel):
    """Resumen de POST /graph/cluster/validate (validador LLM de cúmulos)."""

    blobs: int
    groups: int
    created: int
    synced: int
    dissolved: int
    rejected: int
    promoted: int
    skipped: int
    errors: int
    llm_calls: int
    cost_usd: float


class GraphCluster(BaseModel):
    """Un cúmulo (fila de `relation_clusters`) para GET /graph/clusters."""

    id: int
    status: str
    name: str
    description: str
    confidence: float | None
    member_count: int


class GraphClustersResponse(BaseModel):
    clusters: list[GraphCluster]


class GraphTimelineEvent(BaseModel):
    """Un suceso fechado de la cronología de un cúmulo."""

    slug: str
    id: int
    kind: str
    label: str
    at: str  # ISO en hora local (fecha sola si precision != 'datetime')
    precision: str  # 'datetime' | 'date' | 'inferred'
    source_inbox_ids: list[int]


class GraphTimelineActor(BaseModel):
    """Un miembro sin fecha de evento (elenco/contexto: identidad, hábito)."""

    slug: str
    id: int
    kind: str
    label: str
    source_inbox_ids: list[int]


class GraphClusterTimelineMeta(BaseModel):
    """Cabecera del cúmulo (título + sinopsis de la story)."""

    id: int
    name: str
    description: str
    confidence: float | None
    member_count: int


class GraphClusterTimeline(BaseModel):
    """GET /graph/clusters/{id}/timeline: cabecera + sucesos ordenados + elenco. `inbox_kinds`
    como en `GraphResponse`: medio por id de inbox referenciado por sucesos/elenco."""

    cluster: GraphClusterTimelineMeta
    events: list[GraphTimelineEvent]
    actors: list[GraphTimelineActor]
    inbox_kinds: dict[int, str] = {}


# --- Gate de relevancia (intereses personales, correos) ---
GateMode = Literal["per_window", "per_message"]
GateProvider = Literal["anthropic", "codex"]
#: El QUIÉN de una regla (el patrón del asunto, el QUÉ, es un campo aparte).
SenderKind = Literal["sender_email", "sender_domain", "list_id"]
RuleEffect = Literal["block", "allow"]
GateRuleStatus = Literal["active", "disabled", "rejected"]


class RelevanceGateSettings(BaseModel):
    """Settings del gate (una fila por usuario; sin fila → defaults apagados).

    `mining_min_messages`: umbral de acumulación de la minería (no-relevantes por remitente
    para que esa clase entre al análisis; un solo correo malo nunca propone nada).
    """

    enabled: bool
    mode: GateMode
    model: str
    mining_min_messages: int
    #: 'codex' usa la suscripción del dueño vía `codex exec`: SOLO host-side (las corridas
    #: dentro del contenedor fallan) y sin métricas de tokens (llm_calls a costo 0).
    provider: GateProvider
    codex_model: str | None


class RelevanceGateSettingsPatch(BaseModel):
    """PATCH parcial de los settings del gate (solo los campos presentes)."""

    enabled: bool | None = None
    mode: GateMode | None = None
    model: str | None = None
    mining_min_messages: int | None = Field(default=None, ge=1)
    provider: GateProvider | None = None
    codex_model: str | None = None  # "" limpia el override (default del CLI de codex)


class InterestInfo(BaseModel):
    id: int
    text: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class InterestList(BaseModel):
    items: list[InterestInfo]


class InterestCreateRequest(BaseModel):
    text: str


class InterestPatch(BaseModel):
    """PATCH parcial de un interés (texto y/o enabled)."""

    text: str | None = None
    enabled: bool | None = None


class GateRuleInfo(BaseModel):
    """Una regla determinista del gate (compuesta + bipolar), con su reporte de dry run."""

    id: int
    effect: RuleEffect
    sender_kind: SenderKind | None
    sender_value: str | None
    subject_pattern: str | None
    status: GateRuleStatus
    proposed_by: Literal["llm", "manual"]
    rationale: str
    dry_run_report: dict[str, Any]
    model: str | None
    activated_at: datetime | None
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GateRuleList(BaseModel):
    items: list[GateRuleInfo]


class GateRuleCreateRequest(BaseModel):
    """Alta manual de una regla compuesta (≥1 predicado): corre el dry run; si no pasa → 422.

    `effect` default 'block'. Predicados: remitente (`sender_kind` + `sender_value`) y/o
    `subject_pattern`; al menos uno (lo valida el motor).
    """

    effect: RuleEffect = "block"
    sender_kind: SenderKind | None = None
    sender_value: str | None = None
    subject_pattern: str | None = None
    rationale: str = ""


class GateRulePatch(BaseModel):
    """Toggle reversible de una regla (las `rejected` no se pueden activar)."""

    status: Literal["active", "disabled"]


class MineRulesResponse(BaseModel):
    """Resultado de una corrida de minería de reglas (LLM + dry run por propuesta)."""

    senders: int
    proposed: int
    activated: int
    rejected: int
    skipped: int
    cost_usd: float


class InterestSuggestion(BaseModel):
    """Sugerencia de editar la lista de intereses (segundo lazo: rechazo manual → intereses)."""

    id: int
    action: Literal["add", "remove"]
    text: str
    interest_id: int | None = None
    rationale: str
    status: str
    model: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class InterestSuggestionList(BaseModel):
    items: list[InterestSuggestion] = Field(default_factory=list)


class MineInterestsResponse(BaseModel):
    """Resultado de una corrida del lazo de intereses (LLM sobre las marcas manuales)."""

    marks: int
    proposed: int
    inserted: int
    cost_usd: float


class ResolveSuggestionRequest(BaseModel):
    accept: bool


class RelevanceReviewItem(BaseModel):
    """Un correo en la cola de revisión manual (veredicto `insufficient`)."""

    inbox_id: int
    occurred_at: datetime
    from_email: str | None
    subject: str | None
    snippet: str
    reason: str
    created_at: datetime


class RelevanceReviewList(BaseModel):
    items: list[RelevanceReviewItem]


class RelevanceReviewResolveRequest(BaseModel):
    """Resolución humana de un `insufficient`: escribe la mark + el veredicto (manual)."""

    is_relevant: bool
    reason: str | None = None
