// Tipos de dominio del frontend — espejo de las tablas reales de memex (migraciones
// 0001-0013). Las FILAS van en camelCase (idiomático TS); los PAYLOADS conservan las
// claves snake_case del JSONB porque renderPayload las sniffa tal cual.

// ---- Sources / inbox ----------------------------------------------------------

export type SourceType = "imap" | "telegram" | "social" | "calendar" | "gateway"

export interface Source {
  id: number
  name: string
  type: SourceType
  enabled: boolean
  createdAt: string
  config: Record<string, unknown>
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
}

export interface InboxExtraction {
  /** True aunque finance/calendar estén vacíos: el cursor marca "procesado, sin datos". */
  done: boolean
  modules: string[]
  finance: Record<string, unknown>[]
  calendar: Record<string, unknown>[]
}

export interface InboxLlmCall {
  purpose: string
  model: string
  promptTokens: number
  completionTokens: number
  costUsd: number
  latencyMs: number
  status: string
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
  /** Resultados de fases (solo en el detalle, GET /inbox/{id}). */
  classification?: InboxClassification | null
  summary?: InboxSummary | null
  extraction?: InboxExtraction | null
  llm?: InboxLlmUsage | null
}

export type Tier = "blacklist" | "batch" | "individual"

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
}

export type WorkerJob = "classify" | "summarize" | "extract" | "calendar" | "ocr"
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

export type LogLevel = "info" | "warning" | "error"

export interface LogEvent {
  id: string
  ts: string
  level: LogLevel
  event: string
  module: string
  requestId: string | null
  userId: number | null
  runId: string | null
  sourceId: number | null
  inboxId: number | null
  fields: Record<string, unknown>
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

/** media_assets (migración 0009): la DB guarda solo la REFERENCIA (object_key), nunca el blob. */
export interface MediaAsset {
  id: number
  sha256: string
  objectKey: string
  bucket: string
  contentType: string
  sizeBytes: number
  filename: string | null
  ocrStatus: OcrStatus
  ocrModel: string | null
  /** Lo que "vio" el modelo multimodal (texto OCR). */
  ocrText: string
  ocrError: string | null
  ocrAttempts: number
  /** finish_reason != stop → transcripción cortada. */
  truncated: boolean
  /** Misma imagen ya OCR-eada en otro mensaje (dedup por sha256) → 0 llamadas de visión. */
  dedupHit: boolean
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
  logs: LogEvent[]
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

export interface FinanceExpense {
  id: number
  amount: number
  currency: string
  merchant: string
  /** Categoría DERIVADA (no es columna real de mod_finance_expenses todavía). */
  category: ExpenseCategory
  occurredOn: string // fecha (date)
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
