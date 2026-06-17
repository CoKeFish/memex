// Tipos de dominio del frontend — espejo de las tablas reales de memex (migraciones
// 0001-0013). Las FILAS van en camelCase (idiomático TS); los PAYLOADS conservan las
// claves snake_case del JSONB porque renderPayload las sniffa tal cual.

// ---- Sources / inbox ----------------------------------------------------------

export type SourceType =
  | "imap"
  | "outlook"
  | "telegram"
  | "social"
  | "instagram"
  | "facebook"
  | "x"
  | "calendar"
  | "gateway"
  | "dummy"

/** De dónde resuelve el token de Apify una fuente social (espeja `SourceRow.token_source`):
 * "vault" = secreto cifrado de la cuenta vinculada (pisa al env) · "env" = variable del contenedor
 * (Doppler) · "missing" = no resuelve, el fetch fallará. */
export type TokenSource = "vault" | "env" | "missing"

export interface Source {
  id: number
  name: string
  type: SourceType
  enabled: boolean
  createdAt: string
  config: Record<string, unknown>
  /** Solo redes; null/ausente = tipo sin token reportable. */
  tokenSource?: TokenSource | null
  /** Modos del fetch a demanda que el ingestor HONRA (server-driven; la UI no hardcodea tipos). */
  fetchModes: string[]
  /** Avisos por modo (p. ej. el costo del rango en Instagram), texto listo para mostrar. */
  modeCaveats?: Record<string, string> | null
  /** Categoría conceptual del tipo (server-driven, espeja SourceRow.kind). null/ausente = tipo
   * sin kind registrado (calendar/gateway/dummy). */
  kind?: "email" | "chat" | "social" | null
}

// Payloads fieles (subconjunto usado por la UI). snake_case = claves del JSONB.
export interface EmailPayload {
  from?: { email: string; name?: string | null } | null
  subject?: string | null
  date: string
  body_text: string
  list_id?: string | null
  list_unsubscribe?: string | null
  precedence?: string | null
  auto_submitted?: string | null
  folder: string
  attachments?: { filename: string | null; content_type: string; size: number }[]
}

export interface TelegramPayload {
  chat_id: number
  chat_kind: "group" | "supergroup" | "channel"
  chat_title?: string | null
  message_id: number
  sender?: { user_id: number; username?: string | null; display_name?: string | null } | null
  date: string
  text: string
  reply_to_message_id?: number | null
  forwarded_from?: string | null
  media_kind?: "none" | "photo" | "video" | "document" | "audio" | "voice" | "sticker" | "other"
  media_caption?: string | null
}

export interface SocialPayload {
  platform: "instagram" | "facebook" | "x"
  account: string
  account_name?: string | null
  post_id: string
  url: string
  text: string
  posted_at: string
  media_kind?: "none" | "image" | "video" | "carousel" | "reel" | "other"
}

export type InboxPayload = EmailPayload | TelegramPayload | SocialPayload

export interface InboxClassification {
  tier: string
  metadata?: Record<string, unknown> | null
}

export interface InboxSummary {
  id?: number | null
  tier: string
  content: string
  createdAt?: string | null
  /** `summaries.metadata` del backend; `n` = tamaño real del lote al persistir. */
  metadata?: Record<string, unknown> | null
}

export interface InboxExtraction {
  /** True aunque las listas estén vacías: el cursor marca "procesado, sin datos relevantes". */
  done: boolean
  /** Módulos CONSIDERADOS (con cursor en module_extractions): incluye los ruteados-FUERA. Qué módulos
   *  realmente EXTRAJERON se deriva de la traza (extract_grouped.slugs / extract_<slug>). */
  modules: string[]
  finance: Record<string, unknown>[]
  calendar: Record<string, unknown>[]
  hackathones: Record<string, unknown>[]
  identidades: Record<string, unknown>[]
}

/** Un par candidato de dedup (finance o identidades) con su decisión proc/LLM. Capacidad debug_inbox. */
export interface DedupCandidateDebug {
  reason: string
  score: number | null
  status: string // candidate | confirmed | rejected
  decided_by: string | null // null=proc (FASE 1) · 'llm'=desempate (FASE 2)
  confidence: number | null
  rationale: string | null
  created_at: string | null
  decided_at: string | null
}

/** Estado interno por transacción de finance (seam contraparte→identidad + dedup + consolidación). */
export interface FinanceDebugRow {
  transaction_id: number
  direction: string
  amount: number
  currency: string
  counterparty: string
  counterparty_identity_id: number | null
  counterparty_identity_name: string | null
  occurred_at: string | null
  processing_outcome: string // pending | unique | duplicate
  processed_at: string | null
  consolidated_id: number | null
  is_winner: boolean | null
  dedup_candidates: (DedupCandidateDebug & { other_transaction_id: number })[]
}

/** Estado interno por mención de identidades (resolución + candidatos de merge). */
export interface IdentidadesDebugRow {
  mention_id: number
  mentioned_name: string
  mentioned_kind: string
  resolved_kind: string | null
  resolution_method: string | null
  resolved_identity_id: number | null
  resolved_identity_name: string | null
  confidence: number | null
  created_at: string | null
  merge_candidates: (DedupCandidateDebug & {
    other_identity_id: number
    other_identity_name: string | null
  })[]
}

/** Una llamada LLM INTERNA (dedup fase-2 / co-ocurrencia) correlacionada al mensaje, con su costo
 *  real — estas ops corren en batch con inbox_id=NULL, así que no salen en la traza por-correo. */
export interface InternalLlmCall {
  purpose: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  latency_ms: number
  status: string
  created_at: string | null
  metadata: Record<string, unknown> | null
}

/** Estado interno de un módulo: filas por-entidad + las llamadas LLM internas correlacionadas. */
export interface ModuleDebugData<TRow> {
  rows: TRow[]
  internal_calls: InternalLlmCall[]
}

/** Estado INTERNO por-módulo para la vista de debug (slug → {rows, internal_calls}). debug_inbox. */
export interface ExtractionDebug {
  finance?: ModuleDebugData<FinanceDebugRow>
  identidades?: ModuleDebugData<IdentidadesDebugRow>
}

export interface InboxLlmCall {
  /** Agrupa las llamadas de una misma corrida HTTP; null en corridas batch/CLI. */
  requestId?: string | null
  purpose: string
  model: string
  promptTokens: number
  completionTokens: number
  costUsd: number
  latencyMs: number
  status: string
  errorMessage?: string | null
  createdAt?: string | null
  /** Decisión de la fase: ruteo {slugs_in, chosen}; extracción {items, discarded}. */
  metadata?: Record<string, unknown> | null
}

export interface InboxLlmUsage {
  calls: number
  costUsd: number
  promptTokens: number
  completionTokens: number
  items: InboxLlmCall[]
}

// ---- Traza jerárquica por mensaje (árbol de ejecución, estilo "stack trace") --

/** Tipo de nodo del árbol de traza. El front renderiza GENÉRICO por este campo (no sabe de cada
 *  módulo): `root`/`module` son spans estructurales del orquestador; `entity` referencia una fila de
 *  dominio; `step`/`log`/`decision` son pasos internos; `llm` es una llamada al modelo (lleva su costo
 *  y el output crudo). Un módulo nuevo aparece solo, sin tocar el front. */
export type TraceNodeKind = "root" | "module" | "entity" | "step" | "log" | "decision" | "llm"

/** Un nodo del árbol de traza — lista PLANA con `parentId` (el front arma el árbol). Es la COSTURA
 *  front↔back: `GET /inbox/{id}` devuelve `trace: TraceNodeDto[] | null` con esta forma exacta. El
 *  módulo solo llama `ctx.trace.*` y el backend serializa acá; el front no hardcodea vistas por módulo. */
export interface TraceNodeDto {
  id: number
  parentId: number | null
  /** Orden entre hermanos. */
  seq: number
  kind: TraceNodeKind
  /** Quién lo emitió; null = span del orquestador (route/extract/persist). */
  moduleSlug: string | null
  label: string
  status: "ok" | "warn" | "error" | "info" | null
  /** Nodo de entidad → fila de dominio (tabla + id); el front linkea, NO re-renderiza el dato. */
  ref: { table: string; id: number } | null
  /** Llamada LLM referenciada (solo en nodos `llm`). */
  llmCallId: number | null
  /** Costo propio + acumulado del subárbol (roll-up calculado al leer) y nº de llamadas bajo el nodo. */
  cost: { ownUsd: number; subtreeUsd: number; calls: number }
  /** Señales internas del paso (p. ej. {trgm: 0.82, umbral: 0.90}). */
  detail: Record<string, unknown>
  /** Solo en hojas `llm`: métricas + output CRUDO del modelo (de llm_calls.response_text). */
  llm: {
    model: string
    promptTokens: number
    completionTokens: number
    latencyMs: number
    status: string
    responseText: string | null
  } | null
}

export type FeedbackKind =
  | "missing_data"
  | "missed_important"
  | "bad_summary"
  | "wrong_extraction"
  | "bad_ocr"
  | "other"

/** Feedback manual rápido del usuario sobre un mensaje (solo captura; ver inbox_feedback). */
export interface InboxFeedback {
  kinds: FeedbackKind[]
  note: string | null
  metadata?: Record<string, unknown>
  status: string
  createdAt?: string | null
  updatedAt?: string | null
}

export type FilterAction = "keep" | "ignore" | "archive"

/** Una regla de filtro pre-ingest (filter_rules) — gestión desde el dashboard. */
export interface FilterRule {
  id: number
  sourceType: string | null
  sourceId: number | null
  scope: Record<string, unknown>
  action: FilterAction
  priority: number
  enabled: boolean
}

export interface RelevanceMark {
  isRelevant: boolean
  reason: string | null
  createdAt: string | null
  updatedAt: string | null
}

/** Veredicto del gate de relevancia para un mensaje (`relevance_verdicts`): la CONCLUSIÓN del gate
 *  (¿se procesa?), distinta del tier (dial de costo) y de la marca manual (override). `method` = cómo
 *  se decidió; si fue por regla, la regla compuesta (`ruleEffect` + remitente + asunto) la
 *  identifica. Solo en el detalle. */
export interface RelevanceVerdict {
  verdict: "relevant" | "not_relevant" | "insufficient"
  method: "rule" | "llm" | "manual"
  reason: string | null
  mode: string | null
  model: string | null
  ruleId: number | null
  ruleEffect: "block" | "allow" | null
  ruleSenderKind: string | null
  ruleSenderValue: string | null
  ruleSubjectPattern: string | null
  createdAt: string | null
}

export interface InboxRow {
  id: number
  sourceId: number
  externalId: string
  occurredAt: string
  receivedAt: string
  payload: InboxPayload
  processedAt: string | null
  processError: string | null
  attempts: number
  /** Texto OCR de imágenes adjuntas (etapa memex-ocr), inyectado al render. */
  ocrText?: string
  /** Tier + avance del pipeline: vienen tanto en la lista (indicadores) como en el detalle. */
  classification?: InboxClassification | null
  /** ¿Tiene resumen? / ¿corrió extracción? — flags livianos para el estado en la lista. */
  summarized?: boolean
  extracted?: boolean
  /** Objetos completos de cada fase (solo en el detalle, GET /inbox/{id}). */
  summary?: InboxSummary | null
  extraction?: InboxExtraction | null
  /** Estado interno por-módulo (dedup, seam contraparte→identidad, consolidación) — vista debug.
   *  Camino VIGENTE para mensajes sin árbol por-mensaje: `trace` solo se arma en ventanas de un
   *  mensaje, así que los lotes (chat, correos batch, daemon) siempre se renderizan acá. */
  extractionDebug?: ExtractionDebug | null
  llm?: InboxLlmUsage | null
  /** Árbol de traza jerárquica de la extracción (GET /inbox/{id}). null ⇒ sin árbol por-mensaje:
   *  solo se arma en ventanas de un mensaje, así que lotes/chat caen al fallback
   *  (LlmTrace + extractionDebug). */
  trace?: TraceNodeDto[] | null
  /** Adjuntos del mensaje (media_assets) — solo en el detalle (GET /inbox/{id}). */
  media?: MediaAsset[]
  /** Feedback manual del usuario sobre este mensaje — solo en el detalle. */
  feedback?: InboxFeedback | null
  /** Marca manual de relevancia (override por-mensaje del sistema de calidad) — solo en el detalle. */
  relevance?: RelevanceMark | null
  /** Veredicto del gate de relevancia (la conclusión: ¿se procesa?) — solo en el detalle. */
  relevanceVerdict?: RelevanceVerdict | null
}

export type Tier = "blacklist" | "batch" | "individual"

/** Lote de procesamiento de un mensaje (GET /inbox/{id}/window): "summary" = co-miembros del
 * resumen ya hecho; "prospective" = la ventana que se armaría hoy («Resumir su lote»); "none" =
 * sin lote (blacklist / sin clasificar). Miembros en orden conversacional, incluye al mensaje. */
export interface InboxWindow {
  mode: "summary" | "prospective" | "none"
  summaryId: number | null
  members: InboxRow[]
}

// ---- Observabilidad -----------------------------------------------------------

export type IngestionRunStatus = "running" | "ok" | "failed" | "aborted"

export interface IngestionRun {
  id: string
  sourceId: number
  trigger: string
  status: IngestionRunStatus
  startedAt: string
  endedAt: string | null
  durationMs: number | null
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number // columna agregada en migración 0004
  errorClass: string | null
  errorMessage: string | null
  // Derivado por el backend (corrida 'running' colgada > 30 min). Opcional: las fuentes mock/pipeline
  // no lo traen; /ingest/runs y /ingest/scheduler sí.
  isStale?: boolean
}

export type WorkerJob = "classify" | "extract" | "calendar" | "ocr"
export type WorkerRunStatus = "running" | "ok" | "error"

export interface WorkerRun {
  id: number
  job: WorkerJob
  status: WorkerRunStatus
  stats: Record<string, number | Record<string, number>>
  error: string | null
  startedAt: string
  finishedAt: string | null
}

export type LlmPurpose =
  | "summarize"
  | "extract"
  | "calendar_dedup"
  | "calendar_merge"
  | "ocr"
export type LlmStatus = "ok" | "error" | "filtered"

export interface LlmCall {
  id: number
  requestId: string
  inboxId: number | null
  purpose: LlmPurpose
  model: string
  promptTokens: number
  completionTokens: number
  /** Tokens servidos desde cache (DeepSeek cache_hit). No persistido en DB hoy → solo demo. */
  cacheHitTokens: number
  costUsd: number
  latencyMs: number
  status: LlmStatus
  errorMessage: string | null
  createdAt: string
}

export interface ModelPricing {
  /** USD por 1M tokens. */
  cacheHit: number
  cacheMiss: number
  output: number
  label: string
  /** Modelo no tabulado → cost_usd=0 silencioso (bug a señalar). */
  untabulated?: boolean
}

// ---- Cola de revisión (dead-letter + calendar) --------------------------------

export type FailureStage = "summarize" | "extract"

export interface WorkItemFailure {
  id: number
  stage: FailureStage
  inboxId: number
  attempts: number
  lastError: string | null
  status: "failing" | "review"
  createdAt: string
  updatedAt: string
  /** Preview legible del mensaje original (asunto/cuerpo) para la cola de revisión. */
  preview?: string
}

export interface ConsolidatedEventLite {
  id: number
  title: string
  startsOn: string
  endsOn: string | null
  startTime: string | null
  endTime: string | null
  location: string
  priorityRank: number
  protected: boolean
}

export interface CalendarConflict {
  id: number
  a: ConsolidatedEventLite
  b: ConsolidatedEventLite
  reason: string
  status: "pending" | "resolved" | "dismissed"
  createdAt: string
  /** Instancias agrupadas de un mismo par de series recurrentes (1 = choque único). */
  instanceCount: number
  /** Se repite (>1 instancia) — choque entre dos series recurrentes. */
  recurring: boolean
  /** Rango de fechas del grupo (YYYY-MM-DD). `a`/`b` son la ocurrencia representante. */
  firstOn: string
  lastOn: string
}

export type CalendarOrigin = "extraction" | "provider" | "module"

export interface CalendarEventLite {
  id: number
  title: string
  startsOn: string
  startTime: string | null
  location: string
  origin: CalendarOrigin
  provider: string | null
  /** Mensajes de inbox de los que se extrajo (link al camino de decisión); vacío si es del proveedor. */
  sourceInboxIds: number[]
}

export interface CalendarDedupCandidate {
  id: number
  a: CalendarEventLite
  b: CalendarEventLite
  reason: string
  score: number | null
  status: "candidate" | "confirmed" | "rejected"
  createdAt: string
}

// Item unificado de la bandeja de "pendiente de revisión".
export type ReviewKind = "dead-letter" | "conflict" | "dedup"

export interface ReviewItem {
  id: string
  kind: ReviewKind
  at: string
  deadLetter?: WorkItemFailure
  conflict?: CalendarConflict
  dedup?: CalendarDedupCandidate
}

// ---- Alertas (centro persistente) ---------------------------------------------

export type AlertSeverity = "critica" | "alta" | "info"

export interface AlertEvent {
  id: string
  severity: AlertSeverity
  kind: "saldo" | "worker-stale" | "run-failed" | "source-stale" | "review"
  title: string
  detail: string
  at: string
  read: boolean
  deepLink: string
}

// ---- Cuenta y acceso ----------------------------------------------------------

export interface AccountIdentity {
  userId: number
  email: string
  displayName: string
  createdAt: string
}

export interface ApiEndpoint {
  method: string
  path: string
  auth: boolean
  note?: string
}

export interface ApiAccess {
  authEnforced: boolean
  tokenMasked: string
  resolvesToUserId: number
  endpoints: ApiEndpoint[]
  missing: string[]
}

export interface CliAccess {
  gatewayUrl: string
  tokenMasked: string
  surface: string[]
  namespacing: string
}

export interface ProviderAccount {
  id: number
  provider: string
  accountLabel: string
  calendarId: string
  lastSyncAt: string | null
  syncTokenMasked: string | null
  tokenPathEnv: string
  enabled: boolean
  writeBack: boolean
  tokenState: "delta" | "full-resync" | "never"
}

export interface ImapOAuth {
  sourceName: string
  provider: string
  tokenPathEnv: string
}

export interface Account {
  identity: AccountIdentity
  api: ApiAccess
  cli: CliAccess
  providers: ProviderAccount[]
  imap: ImapOAuth[]
}

// ---- Logs ---------------------------------------------------------------------

export type LogLevel = "debug" | "info" | "warning" | "error" | "critical"

/** Una fila de `log_events` (el sink de structlog, migración 0020) tal como la sirve GET /logs. */
export interface LogEventRow {
  id: number
  ts: string
  level: LogLevel
  event: string
  logger: string | null
  userId: number | null
  requestId: string | null
  runId: string | null
  sourceId: number | null
  inboxId: number | null
  exception: string | null
  fields: Record<string, unknown>
}

export interface LogLevelCount {
  level: LogLevel
  count: number
}

export interface LogEventCount {
  event: string
  count: number
}

export interface LogLoggerCount {
  logger: string
  count: number
}

export interface LogHistogramPoint {
  bucket: string
  total: number
  errors: number
}

export interface LogLatency {
  p50: number | null
  p95: number | null
  p99: number | null
}

/** Agregaciones de GET /logs/stats para el panel de métricas de logs. */
export interface LogStats {
  total: number
  errors: number
  errorRate: number
  byLevel: LogLevelCount[]
  byEvent: LogEventCount[]
  byLogger: LogLoggerCount[]
  histogram: LogHistogramPoint[]
  latency: LogLatency
  sinkDropped: number
}

export type ObsKind = "ingestion" | "worker" | "llm" | "failure" | "calendar"

export interface ObsTimelineEntry {
  id: string
  ts: string
  kind: ObsKind
  title: string
  detail: string
  tone: "ok" | "error" | "running" | "review" | "neutral"
  requestId: string | null
}

// ---- Camino de decisión por mensaje -------------------------------------------

export type OcrStatus = "pending" | "ok" | "error" | "skipped"

/** media_assets (migración 0009/0016): la DB guarda solo la REFERENCIA; el blob va por /media/{id}. */
export interface MediaAsset {
  id: number
  sha256: string
  /** Solo en datos mock; la API real no expone la referencia interna (el blob va por /media/{id}). */
  objectKey?: string
  bucket?: string
  contentType: string
  sizeBytes: number
  filename: string | null
  /** Extensión normalizada (migración 0016): 'pdf' | 'png' | 'zip' | … */
  extension: string | null
  ocrStatus: OcrStatus
  ocrModel: string | null
  /** Lo que "vio" el modelo multimodal (texto OCR). */
  ocrText: string
  ocrError: string | null
  ocrAttempts: number
  /** finish_reason != stop → transcripción cortada. Se deriva de la traza para datos reales. */
  truncated?: boolean
  /** Misma imagen ya OCR-eada en otro mensaje (dedup por sha256) → 0 llamadas de visión. */
  dedupHit?: boolean
}

export type JourneyStepKind =
  | "ingesta"
  | "clasificacion"
  | "ruteo"
  | "modulo"
  | "resumen"
  | "ocr"
  | "deadletter"

/** Intercambio con el LLM en un paso: input resumido + lo que DEVOLVIÓ + métricas. */
export interface LlmExchange {
  purpose: LlmPurpose
  model: string
  promptTokens: number
  completionTokens: number
  costUsd: number
  latencyMs: number
  status: LlmStatus
  inputSummary: string
  output: string
}

export interface JourneyStep {
  kind: JourneyStepKind
  title: string
  at: string
  summary: string
  details: { label: string; value: string }[]
  evidence?: { quote: string; sourceText: string }
  llm?: LlmExchange
  media?: MediaAsset[]
  tone: "ok" | "error" | "running" | "filtered" | "review" | "pending" | "neutral"
}

export interface RelatedRecord {
  table: string
  relation: string
  cardinality: string
  exposedByApi: boolean
  keys: { label: string; value: string }[]
}

export interface MessageJourney {
  row: InboxRow
  steps: JourneyStep[]
  logs: LogEventRow[]
  related: RelatedRecord[]
  media: MediaAsset[]
}

// ---- Módulo finance (vista de dominio) ----------------------------------------

export type ExpenseCategory =
  | "comida"
  | "transporte"
  | "software"
  | "servicios"
  | "educacion"
  | "salud"
  | "entretenimiento"
  | "otros"

/** Sentido del movimiento: plata que SALE (egreso/gasto) vs plata que ENTRA (ingreso/cobro). */
export type FinanceDirection = "ingreso" | "egreso"

export interface FinanceTransaction {
  id: number
  /** 'egreso' = gasto (plata que sale); 'ingreso' = cobro (plata que entra). */
  direction: FinanceDirection
  amount: number
  currency: string
  /** La contraparte (comercio que cobra o pagador que ingresa); cae a `place` o "—" si viene vacía. */
  merchant: string
  /** Categoría de GASTO; los ingresos suelen caer a 'otros'. */
  category: ExpenseCategory
  occurredOn: string // fecha (date)
  description: string
  evidence: string
  sourceInboxIds: number[]
  createdAt: string
  /** Lugar resuelto del catálogo geo (null si el pago no tiene lugar asociado), como en eventos. */
  placeName: string | null
  placeAddress: string | null
}

// ---- Módulo hackathones (extractor puro) --------------------------------------

export type HackathonModality = "presencial" | "online" | "hibrido" | "desconocido"

/** Un hackatón extraído (fila de mod_hackathones_events). Las fechas pueden ser null. */
export interface Hackathon {
  id: number
  name: string
  startsOn: string | null
  endsOn: string | null
  registrationDeadline: string | null
  modality: HackathonModality
  location: string
  url: string
  organizer: string
  technologies: string
  prizes: string
  requirements: string
  description: string
  evidence: string
  sourceInboxIds: number[]
  createdAt: string
}

// ---- Módulo calendar (vista de dominio) ---------------------------------------

export type CalendarOutcome = "unique" | "duplicate" | "shadowed" | "conflict" | "echo"

/** Evento crudo (mod_calendar_events) que compone un consolidado vía event_links. */
export interface CalendarRawMember {
  id: number
  origin: CalendarOrigin
  provider: string | null
  sourceInboxIds: number[]
  evidence: string
  processingOutcome: CalendarOutcome
  /** Si este crudo es el "ganador" (winner_event_id) del consolidado. */
  isWinner: boolean
}

export interface ConsolidatedEvent {
  id: number
  title: string
  startsOn: string
  endsOn: string | null
  startTime: string | null
  endTime: string | null
  location: string
  description: string
  /** Lugar canónico del catálogo geo (FK place_id); null si no resuelto o virtual. */
  placeName: string | null
  placeAddress: string | null
  /** mod_calendar_event_links: cuántos eventos crudos lo componen. */
  memberCount: number
  origins: CalendarOrigin[]
  protected: boolean
  priorityRank: number
  /** Los eventos crudos que lo componen (event_links). */
  members: CalendarRawMember[]
}

export interface DedupDecision {
  id: number
  a: CalendarEventLite
  b: CalendarEventLite
  reason: string
  score: number | null
  status: "candidate" | "confirmed" | "rejected"
  /** Quién decidió: la FASE 2 LLM, una decisión manual, o aún sin decidir. */
  decidedBy: "llm" | "manual" | null
  confidence: number | null
  rationale: string | null
  decidedAt: string | null
}

export interface CalendarSyncRun {
  id: number
  account: string
  direction: "ingress" | "egress"
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  dedupPairs: number
  errors: number
  status: "ok" | "error"
  startedAt: string
  finishedAt: string | null
}

/** Salud de sync de una cuenta (GET /calendar/sync-health), en términos operables. */
export interface CalendarAccountHealth {
  accountId: number
  provider: string
  accountLabel: string
  enabled: boolean
  writeBack: boolean
  cursorState: "incremental" | "full_resync_pendiente" | "sin_primera_sync"
  lastPullAt: string | null
  lastPullStatus: "ok" | "error" | null
  lastPullAgeHours: number | null
  lastPushAt: string | null
  lastPushStatus: "ok" | "error" | null
}

/** ¿La sincronización está funcionando? — la misma fuente que el CLI `sync-status`. */
export interface CalendarSyncHealth {
  overall: "ok" | "desactualizado" | "error" | "nunca" | "sin_cuentas"
  autoSyncActive: boolean
  daemonEnabled: boolean
  calendarJobEnabled: boolean
  lastCycleAt: string | null
  accounts: CalendarAccountHealth[]
}

/** Perillas del módulo calendar (GET/PATCH /calendar/settings). */
export interface CalendarSettings {
  /** ¿Dedup F2 y merge (los pasos que GASTAN LLM) procesan eventos ya vencidos? Default false. */
  llmOnPastEvents: boolean
}

/** Resultado de POST /calendar/accounts/{id}/sync (pull + consolidación, sin LLM ni push). */
export interface CalendarSyncNowResult {
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  dedupPairs: number
  errors: number
  groups: number
  orphans: number
  status: "ok" | "error"
}

// ---- Controles de ingesta / procesamiento -------------------------------------

export interface ModuleSetting {
  slug: string
  label: string
  enabled: boolean
  batchingPolicy: "per_module" | "grouped" | "all"
  groupSize: number
  processed: number
  total: number
}

export interface SchedulerJob {
  job: WorkerJob
  enabled: boolean
  cron: string
  lastRun: string | null
  nextRun: string | null
}

/** Preview de un fetch (dry-run): cuántos correos nuevos vs ya existentes (idempotencia). */
export interface FetchPreview {
  scanned: number
  nuevos: number
  duplicados: number
  filtrados: number
}

export interface RunPreview {
  job: WorkerJob
  pending: number
  estimate: { label: string; value: string }[]
  command: string
}
