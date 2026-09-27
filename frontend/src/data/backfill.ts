// Backfill segmentado (importación masiva por ventanas) contra la API real (router /sources/backfill).
// Espeja `memex.api.routers.backfill`: el `range_end` viaja INCLUSIVO (la fecha "hasta" que eligió el
// usuario); el backend hace la conversión a/desde exclusivo.

import { apiDelete, ApiError, apiGet, apiPost } from "@/lib/api"

export type BackfillWindowUnit = "day" | "week" | "month"

/** Tipos de fuente elegibles para backfill por fecha (espeja `_DATE_WINDOW_TYPES` del backend). */
export const DATE_WINDOW_SOURCE_TYPES = new Set(["imap"])

export interface BackfillWindowResult {
  start: string // YYYY-MM-DD
  end: string // YYYY-MM-DD exclusivo
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  capHit: boolean
  msElapsed: number
  at: string
}

export interface BackfillStateData {
  sourceId: number
  rangeStart: string
  rangeEnd: string // inclusiva (como la eligió el usuario)
  frontier: string
  windowUnit: BackfillWindowUnit
  windowCount: number
  perWindowLimit: number
  status: "active" | "done"
  progressPct: number
  history: BackfillWindowResult[]
}

interface WindowResultApi {
  start: string
  end: string
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  cap_hit: boolean
  ms_elapsed: number
  at: string
}

interface StateApi {
  source_id: number
  range_start: string
  range_end: string
  frontier: string
  window_unit: BackfillWindowUnit
  window_count: number
  per_window_limit: number
  status: "active" | "done"
  progress_pct: number
  history: WindowResultApi[]
}

function toWindow(w: WindowResultApi): BackfillWindowResult {
  return {
    start: w.start,
    end: w.end,
    posted: w.posted,
    inserted: w.inserted,
    duplicates: w.duplicates,
    errors: w.errors,
    filtered: w.filtered,
    capHit: w.cap_hit,
    msElapsed: w.ms_elapsed,
    at: w.at,
  }
}

function toState(s: StateApi): BackfillStateData {
  return {
    sourceId: s.source_id,
    rangeStart: s.range_start,
    rangeEnd: s.range_end,
    frontier: s.frontier,
    windowUnit: s.window_unit,
    windowCount: s.window_count,
    perWindowLimit: s.per_window_limit,
    status: s.status,
    progressPct: s.progress_pct,
    history: s.history.map(toWindow),
  }
}

export interface BackfillConfigInput {
  rangeStart: string
  rangeEnd: string
  windowUnit: BackfillWindowUnit
  windowCount: number
  perWindowLimit?: number
}

/** Estado actual del backfill de una fuente (GET); `null` si no hay ninguno configurado. */
export async function getBackfill(sourceId: number): Promise<BackfillStateData | null> {
  try {
    return toState(await apiGet<StateApi>(`/sources/${sourceId}/backfill`))
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

/** Crea o reconfigura el backfill de la fuente (resetea la frontera al inicio del rango). */
export async function configureBackfill(
  sourceId: number,
  cfg: BackfillConfigInput,
): Promise<BackfillStateData> {
  const body: Record<string, unknown> = {
    range_start: cfg.rangeStart,
    range_end: cfg.rangeEnd,
    window_unit: cfg.windowUnit,
    window_count: cfg.windowCount,
  }
  if (cfg.perWindowLimit != null) body.per_window_limit = cfg.perWindowLimit
  return toState(await apiPost<StateApi>(`/sources/${sourceId}/backfill`, body))
}

export interface BackfillAdvanceResult {
  window: BackfillWindowResult | null
  state: BackfillStateData
  dryRun: boolean
}

interface AdvanceApi {
  window: WindowResultApi | null
  state: StateApi
  dry_run: boolean
}

function toAdvance(a: AdvanceApi): BackfillAdvanceResult {
  return { window: a.window ? toWindow(a.window) : null, state: toState(a.state), dryRun: a.dry_run }
}

export interface AdvanceOpts {
  dryRun?: boolean
  windowUnit?: BackfillWindowUnit
  windowCount?: number
}

/** Procesa la próxima ventana (o la previsualiza con `dryRun`). El override queda como nuevo default. */
export async function advanceBackfill(
  sourceId: number,
  opts?: AdvanceOpts,
): Promise<BackfillAdvanceResult> {
  const qs = opts?.dryRun ? "?dry_run=true" : ""
  const body: Record<string, unknown> = {}
  if (opts?.windowUnit) body.window_unit = opts.windowUnit
  if (opts?.windowCount != null) body.window_count = opts.windowCount
  return toAdvance(await apiPost<AdvanceApi>(`/sources/${sourceId}/backfill/advance${qs}`, body))
}

/** Procesa todo lo que queda hasta el fin del rango en una sola ventana. */
export async function advanceBackfillRest(
  sourceId: number,
  opts?: { dryRun?: boolean },
): Promise<BackfillAdvanceResult> {
  const qs = opts?.dryRun ? "?dry_run=true" : ""
  return toAdvance(await apiPost<AdvanceApi>(`/sources/${sourceId}/backfill/advance-rest${qs}`))
}

/** Borra el backfill de la fuente (reset). */
export async function deleteBackfill(sourceId: number): Promise<void> {
  await apiDelete<void>(`/sources/${sourceId}/backfill`)
}
