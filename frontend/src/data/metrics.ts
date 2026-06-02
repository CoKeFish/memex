// Superficie de MÉTRICAS de costo LLM contra la API real (no mocks). Como `finance.ts`: funciones
// async + transform snake_case → camelCase. A diferencia de finance, el backend YA agrega (GROUP BY
// en /metrics/llm/rollup) porque llm_calls es denso; acá solo traemos el rollup y las filas crudas
// de auditoría (/metrics/llm/calls, con filtros incluir/excluir y orden server-side).

import { apiGet } from "@/lib/api"
import { rangeMs, type RangeKey } from "@/lib/selectors"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface LlmKpis {
  costUsd: number
  calls: number
  promptTokens: number
  completionTokens: number
  cacheHitTokens: number
  cacheHitRatio: number
  avgCostUsd: number
  avgLatencyMs: number
  errors: number
  /** Costo del periodo anterior de igual longitud; null si el rango es "todo". */
  prevCostUsd: number | null
}

export interface SourceCost {
  sourceId: number | null
  sourceName: string
  calls: number
  tokens: number
  costUsd: number
}

export interface ModuleCost {
  module: string
  calls: number
  tokens: number
  costUsd: number
}

export interface ModelCost {
  model: string
  calls: number
  promptTokens: number
  completionTokens: number
  costUsd: number
  /** tokens>0 con costo 0 → modelo sin precio tabulado (gasto silencioso a señalar). */
  untabulated: boolean
}

export interface SourceModuleCost {
  sourceId: number | null
  sourceName: string
  module: string
  calls: number
  costUsd: number
}

export interface DailyCost {
  day: string // 'YYYY-MM-DD'
  total: number
  byModule: Record<string, number>
}

export interface LlmRollup {
  kpis: LlmKpis
  bySource: SourceCost[]
  byModule: ModuleCost[]
  byModel: ModelCost[]
  bySourceModule: SourceModuleCost[]
  daily: DailyCost[]
  /** Módulos presentes en el rango (orden por costo) → series estables del área apilada. */
  modules: string[]
}

export interface LlmCallRow {
  id: number
  createdAt: string
  purpose: string
  module: string
  model: string
  promptTokens: number
  completionTokens: number
  cacheHitTokens: number
  costUsd: number
  latencyMs: number
  status: string
  errorMessage: string | null
  inboxId: number | null
  sourceId: number | null
  sourceName: string | null
  /** Metadata de la fase: extracción {items, discarded, n, ...}; ruteo {chosen, ...}. */
  metadata: Record<string, unknown> | null
}

// ---- Ventana temporal ---------------------------------------------------------------------------

/** Ventana [since, until) en ISO; ambos opcionales (omitir = sin límite por ese lado). */
export interface MetricsWindow {
  since?: string
  until?: string
}

export type RangePreset = "today" | "7d" | "30d" | "90d" | "all"

const DAY = 86_400_000

/** Ventana de un preset. "today" arranca a las 00:00 locales; "all" no acota. */
export function presetWindow(preset: RangePreset): MetricsWindow {
  const now = Date.now()
  switch (preset) {
    case "today": {
      const d = new Date()
      d.setHours(0, 0, 0, 0)
      return { since: d.toISOString() }
    }
    case "7d":
      return { since: new Date(now - 7 * DAY).toISOString() }
    case "30d":
      return { since: new Date(now - 30 * DAY).toISOString() }
    case "90d":
      return { since: new Date(now - 90 * DAY).toISOString() }
    case "all":
      return {}
  }
}

/** Mapea el RangeKey del picker GLOBAL (topbar) a una ventana — lo usa la vista Resumen. */
export function rangeKeyWindow(range: RangeKey): MetricsWindow {
  const span = rangeMs(range)
  if (!Number.isFinite(span)) return {} // "all"
  return { since: new Date(Date.now() - span).toISOString() }
}

/** Ventana de un rango personalizado (inputs date YYYY-MM-DD). `until` se vuelve exclusivo del día
 *  siguiente para incluir el día completo (el backend filtra created_at < until). */
export function customWindow(sinceDay?: string, untilDay?: string): MetricsWindow {
  const w: MetricsWindow = {}
  if (sinceDay) w.since = new Date(`${sinceDay}T00:00:00`).toISOString()
  if (untilDay) w.until = new Date(new Date(`${untilDay}T00:00:00`).getTime() + DAY).toISOString()
  return w
}

// ---- API rows (snake_case) + transforms ---------------------------------------------------------

interface KpisApi {
  cost_usd: number
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cache_hit_tokens: number
  cache_hit_ratio: number
  avg_cost_usd: number
  avg_latency_ms: number
  errors: number
  prev_cost_usd: number | null
}
interface SourceApi { source_id: number | null; source_name: string; calls: number; tokens: number; cost_usd: number }
interface ModuleApi { module: string; calls: number; tokens: number; cost_usd: number }
interface ModelApi { model: string; calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number; untabulated: boolean }
interface SourceModuleApi { source_id: number | null; source_name: string; module: string; calls: number; cost_usd: number }
interface DailyApi { day: string; total: number; by_module: Record<string, number> }
interface RollupApi {
  kpis: KpisApi
  by_source: SourceApi[]
  by_module: ModuleApi[]
  by_model: ModelApi[]
  by_source_module: SourceModuleApi[]
  daily: DailyApi[]
  modules: string[]
}
interface CallApi {
  id: number
  created_at: string
  purpose: string
  module: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  cache_hit_tokens: number
  cost_usd: number
  latency_ms: number
  status: string
  error_message: string | null
  inbox_id: number | null
  source_id: number | null
  source_name: string | null
  metadata: Record<string, unknown> | null
}

function toRollup(r: RollupApi): LlmRollup {
  return {
    kpis: {
      costUsd: r.kpis.cost_usd,
      calls: r.kpis.calls,
      promptTokens: r.kpis.prompt_tokens,
      completionTokens: r.kpis.completion_tokens,
      cacheHitTokens: r.kpis.cache_hit_tokens,
      cacheHitRatio: r.kpis.cache_hit_ratio,
      avgCostUsd: r.kpis.avg_cost_usd,
      avgLatencyMs: r.kpis.avg_latency_ms,
      errors: r.kpis.errors,
      prevCostUsd: r.kpis.prev_cost_usd,
    },
    bySource: r.by_source.map((s) => ({
      sourceId: s.source_id,
      sourceName: s.source_name,
      calls: s.calls,
      tokens: s.tokens,
      costUsd: s.cost_usd,
    })),
    byModule: r.by_module.map((m) => ({ module: m.module, calls: m.calls, tokens: m.tokens, costUsd: m.cost_usd })),
    byModel: r.by_model.map((m) => ({
      model: m.model,
      calls: m.calls,
      promptTokens: m.prompt_tokens,
      completionTokens: m.completion_tokens,
      costUsd: m.cost_usd,
      untabulated: m.untabulated,
    })),
    bySourceModule: r.by_source_module.map((c) => ({
      sourceId: c.source_id,
      sourceName: c.source_name,
      module: c.module,
      calls: c.calls,
      costUsd: c.cost_usd,
    })),
    daily: r.daily.map((d) => ({ day: d.day, total: d.total, byModule: d.by_module })),
    modules: r.modules,
  }
}

function toRow(r: CallApi): LlmCallRow {
  return {
    id: r.id,
    createdAt: r.created_at,
    purpose: r.purpose,
    module: r.module,
    model: r.model,
    promptTokens: r.prompt_tokens,
    completionTokens: r.completion_tokens,
    cacheHitTokens: r.cache_hit_tokens,
    costUsd: r.cost_usd,
    latencyMs: r.latency_ms,
    status: r.status,
    errorMessage: r.error_message,
    inboxId: r.inbox_id,
    sourceId: r.source_id,
    sourceName: r.source_name,
    metadata: r.metadata,
  }
}

function windowParams(qs: URLSearchParams, w: MetricsWindow): void {
  if (w.since) qs.set("since", w.since)
  if (w.until) qs.set("until", w.until)
}

/** Rollup de costo LLM del rango (KPIs + cortes por fuente/módulo/modelo + matriz + serie diaria). */
export async function fetchLlmRollup(w: MetricsWindow = {}): Promise<LlmRollup> {
  const qs = new URLSearchParams()
  windowParams(qs, w)
  return toRollup(await apiGet<RollupApi>(`/metrics/llm/rollup?${qs.toString()}`))
}

export type FilterMode = "include" | "exclude"

export interface LlmCallsQuery extends MetricsWindow {
  status?: string[]
  statusMode?: FilterMode
  module?: string[]
  moduleMode?: FilterMode
  model?: string[]
  modelMode?: FilterMode
  /** Filtra por source_name (incluye los pseudo "(calendar)"/"(sin source)"). */
  source?: string[]
  sourceMode?: FilterMode
  q?: string
  sort?: "created_at" | "cost_usd" | "latency_ms"
  dir?: "asc" | "desc"
  limit?: number
  offset?: number
}

function multiParam(
  qs: URLSearchParams,
  name: string,
  values: string[] | undefined,
  mode: FilterMode | undefined,
): void {
  if (!values || values.length === 0) return
  for (const v of values) qs.append(name, v)
  if (mode === "exclude") qs.set(`${name}_mode`, "exclude")
}

/** Filas crudas de auditoría con filtros incluir/excluir, orden y paginación offset. */
export async function fetchLlmCalls(query: LlmCallsQuery): Promise<{ items: LlmCallRow[]; total: number }> {
  const qs = new URLSearchParams()
  windowParams(qs, query)
  multiParam(qs, "status", query.status, query.statusMode)
  multiParam(qs, "module", query.module, query.moduleMode)
  multiParam(qs, "model", query.model, query.modelMode)
  multiParam(qs, "source", query.source, query.sourceMode)
  if (query.q) qs.set("q", query.q)
  if (query.sort) qs.set("sort", query.sort)
  if (query.dir) qs.set("dir", query.dir)
  qs.set("limit", String(query.limit ?? 50))
  qs.set("offset", String(query.offset ?? 0))
  const r = await apiGet<{ items: CallApi[]; total: number }>(`/metrics/llm/calls?${qs.toString()}`)
  return { items: r.items.map(toRow), total: r.total }
}
