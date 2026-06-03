// Control de la ingesta agendada desde /carga, contra la API real (router /ingest, 0025). Como
// processing.ts: funciones async + transform snake_case → camelCase. Tres superficies:
//   - estado del daemon de ingesta (master toggle + por fuente su schedule y última corrida),
//   - setear el intervalo por fuente (PATCH /sources/{id} fetch_schedule),
//   - historial de corridas (ingestion_runs) con su ORIGEN, para linkear a /logs?run_id=.

import { apiGet, apiPatch } from "@/lib/api"
import type { IngestionRun, IngestionRunStatus } from "@/types/domain"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface IngestScheduleSource {
  sourceId: number
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown> // para sourceMeta (icono/etiqueta de proveedor)
  fetchSchedule: string | null // ISO-8601 (PT1H, P1D…) o null = no agendada
  latest: IngestionRun | null
}

export interface IngestSchedulerState {
  daemonEnabled: boolean
  sources: IngestScheduleSource[]
}

export interface IngestionRunsQuery {
  sourceId?: number
  trigger?: string
  limit?: number
}

// ---- API rows (snake_case) ----------------------------------------------------------------------

interface IngestionRunApi {
  id: string
  source_id: number
  trigger: string
  status: IngestionRunStatus
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  error_class: string | null
  error_message: string | null
  is_stale: boolean
}

interface IngestScheduleSourceApi {
  source_id: number
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown>
  fetch_schedule: string | null
  latest: IngestionRunApi | null
}

interface IngestSchedulerApi {
  daemon_enabled: boolean
  sources: IngestScheduleSourceApi[]
}

// ---- transforms ---------------------------------------------------------------------------------

function toRun(r: IngestionRunApi): IngestionRun {
  return {
    id: r.id,
    sourceId: r.source_id,
    trigger: r.trigger,
    status: r.status,
    startedAt: r.started_at,
    endedAt: r.ended_at,
    durationMs: r.duration_ms,
    posted: r.posted,
    inserted: r.inserted,
    duplicates: r.duplicates,
    errors: r.errors,
    filtered: r.filtered,
    errorClass: r.error_class,
    errorMessage: r.error_message,
    isStale: r.is_stale,
  }
}

function toState(s: IngestSchedulerApi): IngestSchedulerState {
  return {
    daemonEnabled: s.daemon_enabled,
    sources: s.sources.map((x) => ({
      sourceId: x.source_id,
      name: x.name,
      type: x.type,
      enabled: x.enabled,
      config: x.config ?? {},
      fetchSchedule: x.fetch_schedule,
      latest: x.latest ? toRun(x.latest) : null,
    })),
  }
}

// ---- endpoints ----------------------------------------------------------------------------------

/** Estado del daemon de ingesta: master toggle + por fuente su schedule y última corrida. */
export async function fetchIngestScheduler(): Promise<IngestSchedulerState> {
  return toState(await apiGet<IngestSchedulerApi>("/ingest/scheduler"))
}

/** Prende/apaga el master toggle del daemon de ingesta (PATCH /ingest/scheduler). */
export async function setIngestScheduler(daemonEnabled: boolean): Promise<IngestSchedulerState> {
  return toState(await apiPatch<IngestSchedulerApi>("/ingest/scheduler", { daemon_enabled: daemonEnabled }))
}

/** Setea (o limpia con `null`) el intervalo de ingesta de una fuente (PATCH /sources/{id}). */
export async function setSourceSchedule(id: number, schedule: string | null): Promise<void> {
  await apiPatch(`/sources/${id}`, { fetch_schedule: schedule })
}

/** Corridas de ingesta recientes (con su origen), más nuevas primero, para el historial + deep-link. */
export async function fetchIngestionRuns(q: IngestionRunsQuery = {}): Promise<IngestionRun[]> {
  const qs = new URLSearchParams()
  if (q.sourceId != null) qs.set("source_id", String(q.sourceId))
  if (q.trigger) qs.set("trigger", q.trigger)
  qs.set("limit", String(q.limit ?? 20))
  const r = await apiGet<{ items: IngestionRunApi[] }>(`/ingest/runs?${qs.toString()}`)
  return r.items.map(toRun)
}
