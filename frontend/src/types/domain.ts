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
