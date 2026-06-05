// Superficie de CORREOS contra la API real (no mocks). El resto del dashboard sigue en mocks
// vía el barrel `@/data`. Estas funciones son async (a diferencia de los getters mock síncronos).

import { apiGet, apiGetBlob, ApiError, apiPost } from "@/lib/api"
import type {
  ExtractionDebug,
  FeedbackKind,
  InboxExtraction,
  InboxFeedback,
  InboxLlmUsage,
  InboxPayload,
  InboxRow,
  MediaAsset,
  OcrStatus,
  Source,
  SourceType,
  TraceNodeDto,
} from "@/types/domain"

interface FeedbackApi {
  kinds: string[]
  note: string | null
  metadata?: Record<string, unknown>
  status: string
  created_at?: string | null
  updated_at?: string | null
}

function toFeedback(f: FeedbackApi): InboxFeedback {
  return {
    kinds: f.kinds as FeedbackKind[],
    note: f.note ?? null,
    metadata: f.metadata,
    status: f.status,
    createdAt: f.created_at ?? null,
    updatedAt: f.updated_at ?? null,
  }
}

interface SourceApiRow {
  id: number
  user_id: number
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown>
  created_at: string
}

function toSource(r: SourceApiRow): Source {
  return {
    id: r.id,
    name: r.name,
    type: r.type as SourceType,
    enabled: r.enabled,
    createdAt: r.created_at,
    config: r.config,
  }
}

/** Todas las fuentes del usuario (GET /sources). */
export async function fetchSources(): Promise<Source[]> {
  const rows = await apiGet<SourceApiRow[]>("/sources")
  return rows.map(toSource)
}

/** Solo fuentes de correo (imap). */
export async function fetchEmailSources(): Promise<Source[]> {
  return (await fetchSources()).filter((s) => s.type === "imap")
}

/** Tipos de fuente que memex puede TRAER a demanda (tienen ingestor/factory). Espeja
 * `_LAZY_FACTORIES` del backend (memex/sources). `outlook` NO está: es push-only (entra por el
 * cliente local, no se puede pull-ear desde el dashboard). */
export const PULLABLE_SOURCE_TYPES = new Set(["imap", "telegram", "instagram", "facebook", "x"])

/** Tipos de fuente cuya ingesta pega contra una API de paga (Apify) → la UI avisa del costo. */
export const PAID_API_TYPES = new Set(["instagram", "facebook", "x", "social"])

/** Fuentes que se pueden traer a demanda (cualquier tipo con ingestor), no solo correo. */
export async function fetchPullableSources(): Promise<Source[]> {
  return (await fetchSources()).filter((s) => PULLABLE_SOURCE_TYPES.has(s.type))
}

/** Checkpoint actual de una fuente (GET /sources/{id}/checkpoint). El cursor es libre por
 * ingestor; para imap suele traer {uidvalidity, last_uid}. null si la fuente aún no trajo nada. */
export async function fetchSourceCheckpoint(
  sourceId: number,
): Promise<{ cursor: Record<string, unknown> | null }> {
  return apiGet<{ cursor: Record<string, unknown> | null }>(`/sources/${sourceId}/checkpoint`)
}

export interface FetchResult {
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  dry_run: boolean
  ms_elapsed: number
}

export type FetchMode = "incremental" | "range" | "last"

export interface FetchOpts {
  dryRun?: boolean
  mode?: FetchMode
  /** range: YYYY-MM-DD inclusivo */
  since?: string
  /** range: YYYY-MM-DD exclusivo */
  until?: string
  /** last/range: tope de mensajes */
  limit?: number
}

/** Dispara una corrida de ingesta a demanda. `dryRun` cuenta sin escribir. */
export async function triggerFetch(sourceId: number, opts?: FetchOpts): Promise<FetchResult> {
  const qs = new URLSearchParams()
  if (opts?.dryRun) qs.set("dry_run", "true")
  if (opts?.mode) qs.set("mode", opts.mode)
  if (opts?.since) qs.set("since", opts.since)
  if (opts?.until) qs.set("until", opts.until)
  if (opts?.limit != null) qs.set("limit", String(opts.limit))
  const q = qs.toString()
  return apiPost<FetchResult>(`/sources/${sourceId}/fetch${q ? `?${q}` : ""}`)
}

export interface AdHocResult {
  inserted?: boolean | null
  id?: number | null
  reason?: string | null
  would_insert?: boolean | null
  validations?: Record<string, unknown> | null
}

/**
 * Inyecta un registro manual (POST /ingest). `payload` es el JSON tipado de la fuente
 * (p. ej. EmailPayload). `externalId`/`occurredAt` se autogeneran si no se pasan.
 */
export async function ingestAdHoc(
  sourceId: number,
  payload: Record<string, unknown>,
  opts?: { dryRun?: boolean; externalId?: string; occurredAt?: string },
): Promise<AdHocResult> {
  const body = {
    source_id: sourceId,
    external_id: opts?.externalId ?? `manual:${Date.now()}`,
    occurred_at: opts?.occurredAt ?? new Date().toISOString(),
    payload,
  }
  return apiPost<AdHocResult>("/ingest", body, { dryRun: opts?.dryRun })
}

export interface InboxStatsResponse {
  sources: Record<string, { total: number; pending: number; errored: number }>
}

/** Conteos del inbox por fuente (GET /inbox/stats). */
export async function fetchInboxStats(): Promise<InboxStatsResponse> {
  return apiGet<InboxStatsResponse>("/inbox/stats")
}

// ---- Lectura del inbox (GET /inbox, /inbox/{id}) ------------------------------

interface InboxApiRow {
  id: number
  source_id: number
  external_id: string
  occurred_at: string
  received_at: string
  payload: Record<string, unknown>
  processed_at: string | null
  process_error: string | null
  attempts: number
  classification?: { tier: string; metadata?: Record<string, unknown> | null } | null
  summarized?: boolean
  extracted?: boolean
  summary?: { id?: number | null; tier: string; content: string; created_at?: string | null } | null
  extraction?: InboxExtraction | null
  extraction_debug?: ExtractionDebug | null
  /** Árbol de traza jerárquica (camelCase, ya en la forma que consume el front). null ⇒ fallback. */
  trace?: TraceNodeDto[] | null
  llm?: {
    calls: number
    cost_usd: number
    prompt_tokens: number
    completion_tokens: number
    items: {
      request_id?: string | null
      purpose: string
      model: string
      prompt_tokens: number
      completion_tokens: number
      cost_usd: number
      latency_ms: number
      status: string
      error_message?: string | null
      created_at?: string | null
      metadata?: Record<string, unknown> | null
    }[]
  } | null
  media?: {
    id: number
    sha256: string
    content_type: string
    filename: string | null
    extension: string | null
    size_bytes: number
    ocr_status: string
    ocr_model: string | null
    ocr_text: string | null
    ocr_error: string | null
    ocr_attempts: number
    ocr_done_at?: string | null
  }[]
  feedback?: FeedbackApi | null
}

function toMediaAsset(m: NonNullable<InboxApiRow["media"]>[number]): MediaAsset {
  return {
    id: m.id,
    sha256: m.sha256,
    contentType: m.content_type,
    sizeBytes: m.size_bytes,
    filename: m.filename,
    extension: m.extension,
    ocrStatus: m.ocr_status as OcrStatus,
    ocrModel: m.ocr_model,
    ocrText: m.ocr_text ?? "",
    ocrError: m.ocr_error,
    ocrAttempts: m.ocr_attempts,
  }
}

function toLlmUsage(l: InboxApiRow["llm"]): InboxLlmUsage | null {
  if (!l) return null
  return {
    calls: l.calls,
    costUsd: l.cost_usd,
    promptTokens: l.prompt_tokens,
    completionTokens: l.completion_tokens,
    items: l.items.map((c) => ({
      requestId: c.request_id ?? null,
      purpose: c.purpose,
      model: c.model,
      promptTokens: c.prompt_tokens,
      completionTokens: c.completion_tokens,
      costUsd: c.cost_usd,
      latencyMs: c.latency_ms,
      status: c.status,
      errorMessage: c.error_message ?? null,
      createdAt: c.created_at ?? null,
      metadata: c.metadata ?? null,
    })),
  }
}

function toInboxRow(r: InboxApiRow): InboxRow {
  const media = (r.media ?? []).map(toMediaAsset)
  // OCR concatenado (assets ok) → el preview "Input al LLM" muestra lo que ve el LLM (espeja el
  // string_agg del backend en orchestrator._load_one_workrow).
  const ocrText = media
    .filter((m) => m.ocrStatus === "ok" && m.ocrText.trim())
    .map((m) => m.ocrText.trim())
    .join("\n")
  return {
    id: r.id,
    sourceId: r.source_id,
    externalId: r.external_id,
    occurredAt: r.occurred_at,
    receivedAt: r.received_at,
    // El backend devuelve el payload como dict arbitrario; renderPayload sniffa las claves
    // en runtime (no depende del tipo estático), así que casteamos vía unknown.
    payload: r.payload as unknown as InboxPayload,
    processedAt: r.processed_at,
    processError: r.process_error,
    attempts: r.attempts,
    ocrText: ocrText || undefined,
    classification: r.classification ?? null,
    summarized: r.summarized ?? false,
    extracted: r.extracted ?? false,
    summary: r.summary
      ? { id: r.summary.id ?? null, tier: r.summary.tier, content: r.summary.content, createdAt: r.summary.created_at ?? null }
      : null,
    extraction: r.extraction ?? null,
    extractionDebug: r.extraction_debug ?? null,
    trace: r.trace ?? null,
    llm: toLlmUsage(r.llm),
    media,
    feedback: r.feedback ? toFeedback(r.feedback) : null,
  }
}

/** Registra feedback rápido de un mensaje — POST /inbox/{id}/feedback. */
export async function reportFeedback(
  id: number,
  body: { kinds: string[]; note?: string | null },
): Promise<InboxFeedback> {
  return toFeedback(await apiPost<FeedbackApi>(`/inbox/${id}/feedback`, body))
}

/** Override manual del tier de clasificación — POST /inbox/{id}/classification. */
export async function setClassification(
  id: number,
  tier: string,
): Promise<{ tier: string; metadata?: Record<string, unknown> | null }> {
  return apiPost<{ tier: string; metadata?: Record<string, unknown> | null }>(
    `/inbox/${id}/classification`,
    { tier },
  )
}

/**
 * Object URL del blob original de un adjunto (GET /media/{id}). El caller debe revocarlo con
 * `URL.revokeObjectURL` al desmontar. `download` fuerza la descarga (Content-Disposition attachment).
 * Se baja con el mismo Bearer que el resto (por eso no se puede usar `<img src>` directo).
 */
export async function fetchMediaBlobUrl(id: number, opts?: { download?: boolean }): Promise<string> {
  const blob = await apiGetBlob(`/media/${id}${opts?.download ? "?download=true" : ""}`)
  return URL.createObjectURL(blob)
}

export type ProcessScope = "individual" | "window"

export interface SummarizeResult {
  status: string
  messages?: number
  content?: string | null
  tier?: string | null
  calls?: number
  cost_usd?: number
  prompt_tokens?: number
  completion_tokens?: number
}

export interface ExtractResult {
  status: string
  items?: number
  discarded?: number
  by_module?: Record<string, number>
  done?: boolean
  modules?: string[]
  finance?: Record<string, unknown>[]
  calendar?: Record<string, unknown>[]
  hackathones?: Record<string, unknown>[]
  calls?: number
  cost_usd?: number
  prompt_tokens?: number
  completion_tokens?: number
}

function phaseQuery(opts?: { scope?: ProcessScope; force?: boolean }): string {
  const qs = new URLSearchParams()
  if (opts?.scope) qs.set("scope", opts.scope)
  if (opts?.force) qs.set("force", "true")
  const q = qs.toString()
  return q ? `?${q}` : ""
}

/** Resume (LLM) un mensaje o su ventana — POST /inbox/{id}/summarize. */
export async function summarizeInboxItem(
  id: number,
  opts?: { scope?: ProcessScope; force?: boolean },
): Promise<SummarizeResult> {
  return apiPost<SummarizeResult>(`/inbox/${id}/summarize${phaseQuery(opts)}`)
}

/** Extrae (módulos finance/calendar, LLM) sobre un mensaje o su ventana — POST /inbox/{id}/extract. */
export async function extractInboxItem(
  id: number,
  opts?: { scope?: ProcessScope; force?: boolean },
): Promise<ExtractResult> {
  return apiPost<ExtractResult>(`/inbox/${id}/extract${phaseQuery(opts)}`)
}

export interface ProcessResult {
  inbox_id: number
  tier: string
  reason: string
  classified: boolean
  already: boolean
}

export interface ReprocessResult {
  targets: number
  stages: string[]
  /** Resultado por etapa: {media:{assets_created,…}, ocr:{ok,…}, …} o {<stage>:{error}}. */
  results: Record<string, Record<string, unknown>>
}

/** Re-aplica etapas (media/ocr/classify/summarize/extract) a un mensaje — POST /inbox/{id}/reprocess. */
export async function reprocessInboxItem(
  id: number,
  stages: string[],
  force = false,
): Promise<ReprocessResult> {
  return apiPost<ReprocessResult>(`/inbox/${id}/reprocess`, { stages, force })
}

/** Procesa (clasifica) un mensaje puntual — POST /inbox/{id}/process. Determinista, sin LLM. */
export async function processInboxItem(id: number): Promise<ProcessResult> {
  return apiPost<ProcessResult>(`/inbox/${id}/process`)
}

interface InboxApiList {
  items: InboxApiRow[]
  next_cursor: number | null
}

export interface FetchInboxOpts {
  sourceId?: number
  processed?: "true" | "false" | "all"
  /** Tope de items acumulados; el backend ordena por id ASC, paginamos por cursor. */
  max?: number
}

/**
 * Trae filas del inbox real. Pagina por `cursor` (limit 500/página) acumulando hasta `max`.
 * El backend ordena por id ASC; el componente reordena por occurredAt desc. Si una fuente
 * supera `max`, se trunca (el componente avisa).
 */
export async function fetchInbox(opts?: FetchInboxOpts): Promise<InboxRow[]> {
  const max = opts?.max ?? 2000
  const pageSize = 500
  const out: InboxRow[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams()
    if (opts?.sourceId != null) qs.set("source_id", String(opts.sourceId))
    if (opts?.processed && opts.processed !== "all") qs.set("processed", opts.processed)
    qs.set("limit", String(pageSize))
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<InboxApiList>(`/inbox?${qs.toString()}`)
    out.push(...page.items.map(toInboxRow))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}

/** Un registro del inbox por id (GET /inbox/{id}); 404 → null. */
export async function fetchInboxItem(id: number): Promise<InboxRow | null> {
  try {
    return toInboxRow(await apiGet<InboxApiRow>(`/inbox/${id}`))
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}
