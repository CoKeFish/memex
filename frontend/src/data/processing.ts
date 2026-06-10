// Controles de /procesamiento contra la API real (routers /modules y /processing). Reemplaza los
// getters mock (getSources/getModuleSettings/getScheduler*/dryRunRun). Como pipeline.ts: funciones
// async + transform snake_case → camelCase. `fetchSources` vive en ./email (se reusa); acá va el
// resto: toggle de fuentes, módulos (toggle + cobertura), scheduler (estado + control) y las
// corridas por lote (dry-run, run en background, polling de /runs).

import { apiGet, apiPatch, apiPost } from "@/lib/api"
import type { Source, SourceType, WorkerJob, WorkerRunStatus } from "@/types/domain"

// ---- Catálogo de etapas / filtros (para el form "qué procesar") ---------------------------------

/** Etapas del pipeline en orden de dependencia (espeja `STAGE_ORDER` del backend). */
export const PROCESSING_STAGES: { key: ProcessingStage; label: string; llm: boolean }[] = [
  { key: "media", label: "Adjuntos (re-bajar)", llm: false },
  { key: "ocr", label: "OCR", llm: true },
  { key: "classify", label: "Clasificar", llm: false },
  { key: "summarize", label: "Resumir", llm: true },
  { key: "extract", label: "Extraer", llm: true },
]

export const PROCESSING_ONLY: { key: ProcessingOnly; label: string }[] = [
  { key: "unstored-attachments", label: "Adjuntos sin guardar" },
  { key: "errored", label: "Con error" },
]

export type ProcessingStage = "media" | "ocr" | "classify" | "summarize" | "extract"
export type ProcessingOnly = "unstored-attachments" | "errored"
export type BatchingPolicy = "per_module" | "grouped" | "all"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface ModuleRow {
  slug: string
  label: string
  enabled: boolean
  batchingPolicy: BatchingPolicy
  groupSize: number
  processed: number
  total: number
  pending: number
}

export interface ModulePatch {
  enabled?: boolean
  batchingPolicy?: BatchingPolicy
  groupSize?: number
}

export interface SchedulerWorkerRun {
  startedAt: string
  finishedAt: string | null
  status: WorkerRunStatus
  stats: Record<string, unknown>
  error: string | null
}

export interface SchedulerJobState {
  name: WorkerJob
  defaultInterval: string
  enabled: boolean
  latest: SchedulerWorkerRun | null
  isStale: boolean
}

export interface SchedulerState {
  daemonEnabled: boolean
  enabledJobs: string[]
  jobs: SchedulerJobState[]
}

export interface SchedulerPatch {
  daemonEnabled?: boolean
  /** CSV de jobs habilitados (mismo formato que el backend). */
  enabledJobs?: string
}

export interface ProcessingRunRequest {
  stages: ProcessingStage[]
  sourceId?: number | null
  since?: string | null // YYYY-MM-DD
  until?: string | null // YYYY-MM-DD
  limit?: number | null
  only?: ProcessingOnly | null
  force?: boolean
}

export interface ProcessingDryRunResult {
  count: number
  sampleIds: number[]
  stages: string[]
}

export interface ProcessingRunStatus {
  runId: number | null
  status: string // running | empty
  count: number
  stages: string[]
}

/** Resultado por etapa de `reprocess()` (cada etapa trae sus propios contadores o `{error}`). */
export type StageResult = Record<string, number | string>

export interface ProcessingRun {
  id: number
  status: WorkerRunStatus
  stats: { targets?: number; stages?: string[]; results?: Record<string, StageResult> }
  error: string | null
  startedAt: string
  finishedAt: string | null
  runConfig: {
    stages?: string[]
    targets?: number[]
    force?: boolean
    filters?: Record<string, unknown>
  }
  isStale: boolean
}

// ---- API rows (snake_case) ----------------------------------------------------------------------

interface SourceApiRow {
  id: number
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown>
  created_at: string
}
interface ModuleApi {
  slug: string
  label: string
  enabled: boolean
  batching_policy: BatchingPolicy
  group_size: number
  processed: number
  total: number
  pending: number
}
interface WorkerRunApi {
  started_at: string
  finished_at: string | null
  status: WorkerRunStatus
  stats: Record<string, unknown>
  error: string | null
}
interface SchedulerJobApi {
  name: WorkerJob
  default_interval: string
  enabled: boolean
  latest: WorkerRunApi | null
  is_stale: boolean
}
interface SchedulerApi {
  daemon_enabled: boolean
  enabled_jobs: string[]
  jobs: SchedulerJobApi[]
}
interface ProcessingRunApi {
  id: number
  status: WorkerRunStatus
  stats: ProcessingRun["stats"]
  error: string | null
  started_at: string
  finished_at: string | null
  run_config: ProcessingRun["runConfig"]
  is_stale: boolean
}

// ---- transforms ---------------------------------------------------------------------------------

function toSource(r: SourceApiRow): Source {
  return {
    id: r.id,
    name: r.name,
    type: r.type as SourceType,
    enabled: r.enabled,
    createdAt: r.created_at,
    config: r.config,
    fetchModes: ["incremental"], // esta vista no dispara fetches con ventana; default seguro
  }
}

function toModule(m: ModuleApi): ModuleRow {
  return {
    slug: m.slug,
    label: m.label,
    enabled: m.enabled,
    batchingPolicy: m.batching_policy,
    groupSize: m.group_size,
    processed: m.processed,
    total: m.total,
    pending: m.pending,
  }
}

function toWorkerRun(w: WorkerRunApi | null): SchedulerWorkerRun | null {
  return w
    ? {
        startedAt: w.started_at,
        finishedAt: w.finished_at,
        status: w.status,
        stats: w.stats,
        error: w.error,
      }
    : null
}

function toScheduler(s: SchedulerApi): SchedulerState {
  return {
    daemonEnabled: s.daemon_enabled,
    enabledJobs: s.enabled_jobs,
    jobs: s.jobs.map((j) => ({
      name: j.name,
      defaultInterval: j.default_interval,
      enabled: j.enabled,
      latest: toWorkerRun(j.latest),
      isStale: j.is_stale,
    })),
  }
}

function toRun(r: ProcessingRunApi): ProcessingRun {
  return {
    id: r.id,
    status: r.status,
    stats: r.stats ?? {},
    error: r.error,
    startedAt: r.started_at,
    finishedAt: r.finished_at,
    runConfig: r.run_config ?? {},
    isStale: r.is_stale,
  }
}

function toRunBody(r: ProcessingRunRequest): Record<string, unknown> {
  return {
    stages: r.stages,
    source_id: r.sourceId ?? null,
    since: r.since || null,
    until: r.until || null,
    limit: r.limit ?? null,
    only: r.only ?? null,
    force: r.force ?? false,
  }
}

// ---- fuentes ------------------------------------------------------------------------------------

/** Togglea `sources.enabled` (PATCH /sources/{id}). `fetchSources` vive en ./email. */
export async function setSourceEnabled(id: number, enabled: boolean): Promise<Source> {
  return toSource(await apiPatch<SourceApiRow>(`/sources/${id}`, { enabled }))
}

// ---- módulos ------------------------------------------------------------------------------------

export async function fetchModules(): Promise<ModuleRow[]> {
  const r = await apiGet<{ items: ModuleApi[] }>("/modules")
  return r.items.map(toModule)
}

export async function setModule(slug: string, patch: ModulePatch): Promise<ModuleRow> {
  const body: Record<string, unknown> = {}
  if (patch.enabled !== undefined) body.enabled = patch.enabled
  if (patch.batchingPolicy !== undefined) body.batching_policy = patch.batchingPolicy
  if (patch.groupSize !== undefined) body.group_size = patch.groupSize
  return toModule(await apiPatch<ModuleApi>(`/modules/${slug}`, body))
}

// ---- scheduler ----------------------------------------------------------------------------------

export async function fetchScheduler(): Promise<SchedulerState> {
  return toScheduler(await apiGet<SchedulerApi>("/processing/scheduler"))
}

export async function setScheduler(patch: SchedulerPatch): Promise<SchedulerState> {
  const body: Record<string, unknown> = {}
  if (patch.daemonEnabled !== undefined) body.daemon_enabled = patch.daemonEnabled
  if (patch.enabledJobs !== undefined) body.enabled_jobs = patch.enabledJobs
  return toScheduler(await apiPatch<SchedulerApi>("/processing/scheduler", body))
}

// ---- corridas por lote --------------------------------------------------------------------------

export async function dryRunProcessing(req: ProcessingRunRequest): Promise<ProcessingDryRunResult> {
  const r = await apiPost<{ count: number; sample_ids: number[]; stages: string[] }>(
    "/processing/dry-run",
    toRunBody(req),
  )
  return { count: r.count, sampleIds: r.sample_ids, stages: r.stages }
}

export async function runProcessing(req: ProcessingRunRequest): Promise<ProcessingRunStatus> {
  const r = await apiPost<{ run_id: number | null; status: string; count: number; stages: string[] }>(
    "/processing/run",
    toRunBody(req),
  )
  return { runId: r.run_id, status: r.status, count: r.count, stages: r.stages }
}

export async function fetchProcessingRuns(limit = 20): Promise<ProcessingRun[]> {
  const r = await apiGet<{ items: ProcessingRunApi[] }>(`/processing/runs?limit=${limit}`)
  return r.items.map(toRun)
}
