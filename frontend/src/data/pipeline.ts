// Observabilidad del pipeline contra la API real (router /stats). Reemplaza los selectores mock
// (sourceHealth/workerLatest/ingestion*) que la vista /pipeline y los contadores del /resumen usaban
// sobre seeds. Como metrics.ts: funciones async + transform snake_case → camelCase. El backend ya
// agrega server-side (ingestion_runs, worker_runs, work_item_failures, mod_calendar_conflicts, inbox).

import { apiGet } from "@/lib/api"
import type { MetricsWindow } from "./metrics"
import type { AlertEvent, IngestionRunStatus, SourceType, WorkerRunStatus } from "@/types/domain"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface SourceLastRun {
  startedAt: string
  endedAt: string | null
  status: IngestionRunStatus
  errorClass: string | null
  errorMessage: string | null
}

export interface SourceHealthRow {
  sourceId: number
  name: string
  type: SourceType
  enabled: boolean
  alias: string | null
  /** Identidad real de la cuenta/buzón (el email): de la cuenta server-side o reportado por el
   * cliente local. Para rotular de qué correo es. */
  accountEmail: string | null
  lastRun: SourceLastRun | null
  successRate: number
  totalInserted: number
  totalFiltered: number
  /** Insertados de las últimas corridas, viejo→nuevo (para el sparkline). */
  recent: number[]
}

export interface WorkerLatestRun {
  startedAt: string
  finishedAt: string | null
  status: WorkerRunStatus
  stats: Record<string, unknown>
  error: string | null
}

export interface WorkerLatestRow {
  /** El nombre del job (classify/summarize/extract/ocr/calendar; un job extra se muestra crudo). */
  job: string
  latest: WorkerLatestRun | null
  isStale: boolean
}

export interface IngestionRunRow {
  id: string
  sourceId: number
  /** Nombre de la fuente (JOIN); null si la fuente fue borrada. */
  sourceName: string | null
  trigger: string
  status: IngestionRunStatus
  startedAt: string
  endedAt: string | null
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  errorClass: string | null
  errorMessage: string | null
  /** Gasto Apify de la corrida (agregado de sus runs de actor); null = sin API paga. */
  apiCostUsd: number | null
  expected: number
  balanced: boolean
}

export interface IngestionTotalsRow {
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  runs: number
  unbalanced: number
  /** Gasto Apify sumado de las corridas listadas. */
  api_cost_usd: number
}

export interface PipelineStats {
  sources: SourceHealthRow[]
  workers: WorkerLatestRow[]
  ingestion: { runs: IngestionRunRow[]; totals: IngestionTotalsRow }
}

export interface ReviewCounts {
  deadLetter: number
  calendarConflicts: number
  total: number
}

export interface OverviewStats {
  review: ReviewCounts
  inboxPending: number
  inboxErrors: number
  staleWorkers: number
}

// ---- API rows (snake_case) ----------------------------------------------------------------------

interface SourceRunApi {
  started_at: string
  ended_at: string | null
  status: IngestionRunStatus
  error_class: string | null
  error_message: string | null
}
interface SparkApi { started_at: string; inserted: number }
interface SourceHealthApi {
  source_id: number
  name: string
  type: SourceType
  enabled: boolean
  alias: string | null
  account_email: string | null
  last_run: SourceRunApi | null
  success_rate: number
  total_inserted: number
  total_filtered: number
  recent: SparkApi[]
}
interface WorkerRunApi {
  started_at: string
  finished_at: string | null
  status: WorkerRunStatus
  stats: Record<string, unknown>
  error: string | null
}
interface WorkerLatestApi { job: string; latest: WorkerRunApi | null; is_stale: boolean }
interface IngestionRunApi {
  id: string
  source_id: number
  source_name: string | null
  trigger: string
  status: IngestionRunStatus
  started_at: string
  ended_at: string | null
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  error_class: string | null
  error_message: string | null
  api_cost_usd: number | null
  expected: number
  balanced: boolean
}
interface PipelineApi {
  sources: SourceHealthApi[]
  workers: WorkerLatestApi[]
  ingestion: { runs: IngestionRunApi[]; totals: IngestionTotalsRow }
}
interface ReviewApi { dead_letter: number; calendar_conflicts: number; total: number }
interface OverviewApi {
  review: ReviewApi
  inbox_pending: number
  inbox_errors: number
  stale_workers: number
}

// ---- transforms ---------------------------------------------------------------------------------

function toSource(s: SourceHealthApi): SourceHealthRow {
  return {
    sourceId: s.source_id,
    name: s.name,
    type: s.type,
    enabled: s.enabled,
    alias: s.alias,
    accountEmail: s.account_email,
    lastRun: s.last_run
      ? {
          startedAt: s.last_run.started_at,
          endedAt: s.last_run.ended_at,
          status: s.last_run.status,
          errorClass: s.last_run.error_class,
          errorMessage: s.last_run.error_message,
        }
      : null,
    successRate: s.success_rate,
    totalInserted: s.total_inserted,
    totalFiltered: s.total_filtered,
    recent: s.recent.map((p) => p.inserted),
  }
}

function toWorker(w: WorkerLatestApi): WorkerLatestRow {
  return {
    job: w.job,
    isStale: w.is_stale,
    latest: w.latest
      ? {
          startedAt: w.latest.started_at,
          finishedAt: w.latest.finished_at,
          status: w.latest.status,
          stats: w.latest.stats,
          error: w.latest.error,
        }
      : null,
  }
}

function toRun(r: IngestionRunApi): IngestionRunRow {
  return {
    id: r.id,
    sourceId: r.source_id,
    sourceName: r.source_name,
    trigger: r.trigger,
    status: r.status,
    startedAt: r.started_at,
    endedAt: r.ended_at,
    posted: r.posted,
    inserted: r.inserted,
    duplicates: r.duplicates,
    errors: r.errors,
    filtered: r.filtered,
    errorClass: r.error_class,
    errorMessage: r.error_message,
    apiCostUsd: r.api_cost_usd,
    expected: r.expected,
    balanced: r.balanced,
  }
}

/** Salud por fuente + estado de workers + corridas de ingesta del rango (vista /pipeline). */
export async function fetchPipeline(w: MetricsWindow = {}): Promise<PipelineStats> {
  const qs = new URLSearchParams()
  if (w.since) qs.set("since", w.since)
  if (w.until) qs.set("until", w.until)
  const r = await apiGet<PipelineApi>(`/stats/pipeline?${qs.toString()}`)
  return {
    sources: r.sources.map(toSource),
    workers: r.workers.map(toWorker),
    ingestion: { runs: r.ingestion.runs.map(toRun), totals: r.ingestion.totals },
  }
}

/** Contadores del /resumen: pendientes de revisión, inbox sin procesar / con error, workers colgados. */
export async function fetchOverview(): Promise<OverviewStats> {
  const r = await apiGet<OverviewApi>("/stats/overview")
  return {
    review: {
      deadLetter: r.review.dead_letter,
      calendarConflicts: r.review.calendar_conflicts,
      total: r.review.total,
    },
    inboxPending: r.inbox_pending,
    inboxErrors: r.inbox_errors,
    staleWorkers: r.stale_workers,
  }
}

// ---- Alertas (derivadas de la observabilidad real: GET /stats/alerts) ---------------------------

interface AlertApi {
  id: string
  severity: AlertEvent["severity"]
  kind: AlertEvent["kind"]
  title: string
  detail: string
  at: string
  read: boolean
  deep_link: string
}

/** Alertas REALES (ingesta fallida, worker colgado/en error, backlog de revisión). [] = todo ok. */
export async function fetchAlerts(): Promise<AlertEvent[]> {
  const rows = await apiGet<AlertApi[]>("/stats/alerts")
  return rows.map((a) => ({
    id: a.id,
    severity: a.severity,
    kind: a.kind,
    title: a.title,
    detail: a.detail,
    at: a.at,
    read: a.read,
    deepLink: a.deep_link,
  }))
}
