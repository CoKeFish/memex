// Superficie de MÉTRICAS de costo LLM contra la API real (no mocks). Como `finance.ts`: funciones
// async + transform snake_case → camelCase. A diferencia de finance, el backend YA agrega (GROUP BY
// en /metrics/llm/rollup) porque llm_calls es denso; acá solo traemos el rollup y las filas crudas
// de auditoría (/metrics/llm/calls, con filtros incluir/excluir y orden server-side).

import { apiGet } from "@/lib/api"
import { rangeMs, type RangeKey } from "@/lib/selectors"
import { startOfDayInTz, todayInTz } from "@/lib/timezone"

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
  /** #Llamadas del periodo anterior; null si no hay `since`. 0 distingue "previo vacío" del delta. */
  prevCalls: number | null
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

/** Ventana [since, until) en ISO; ambos opcionales (omitir = sin límite por ese lado). `tz` (IANA)
 *  es la zona de display: el cliente computa "hoy"/custom como medianoche-en-`tz` y la pasa al
 *  backend para que el bucket diario coincida con el reloj de pared del usuario. */
export interface MetricsWindow {
  since?: string
  until?: string
  tz?: string
}

export type RangePreset = "today" | "7d" | "30d" | "90d" | "all"

const DAY = 86_400_000

/** Ventana de un preset, anclada a `tz`. "today" arranca en la medianoche de `tz` (no la local del
 *  navegador); 7d/30d/90d son instantes relativos (TZ-agnósticos); "all" no acota. */
export function presetWindow(preset: RangePreset, tz: string): MetricsWindow {
  const now = Date.now()
  switch (preset) {
    case "today": {
      const { y, m, d } = todayInTz(tz)
      return { since: startOfDayInTz(y, m, d, tz), tz }
    }
    case "7d":
      return { since: new Date(now - 7 * DAY).toISOString(), tz }
    case "30d":
      return { since: new Date(now - 30 * DAY).toISOString(), tz }
    case "90d":
      return { since: new Date(now - 90 * DAY).toISOString(), tz }
    case "all":
      return { tz }
  }
}

/** Mapea el RangeKey del picker GLOBAL (topbar) a una ventana — lo usa la vista Resumen. */
export function rangeKeyWindow(range: RangeKey): MetricsWindow {
  const span = rangeMs(range)
  if (!Number.isFinite(span)) return {} // "all"
  return { since: new Date(Date.now() - span).toISOString() }
}

/** Ventana de un rango personalizado (inputs date YYYY-MM-DD) anclada a `tz`. Cada día arranca en
 *  la medianoche de `tz` (no la local del navegador); `until` se vuelve exclusivo del día siguiente
 *  para incluir el día completo (el backend filtra created_at < until). */
export function customWindow(sinceDay: string | undefined, untilDay: string | undefined, tz: string): MetricsWindow {
  const w: MetricsWindow = { tz }
  if (sinceDay) {
    const [y, m, d] = sinceDay.split("-").map(Number)
    w.since = startOfDayInTz(y, m, d, tz)
  }
  if (untilDay) {
    const [y, m, d] = untilDay.split("-").map(Number)
    w.until = new Date(new Date(startOfDayInTz(y, m, d, tz)).getTime() + DAY).toISOString()
  }
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
  prev_calls: number | null
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
      prevCalls: r.kpis.prev_calls,
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

export function windowParams(qs: URLSearchParams, w: MetricsWindow): void {
  if (w.since) qs.set("since", w.since)
  if (w.until) qs.set("until", w.until)
  if (w.tz) qs.set("tz", w.tz)
}

/** Rollup de costo LLM del rango (KPIs + cortes por fuente/módulo/modelo + matriz + serie diaria). */
export async function fetchLlmRollup(w: MetricsWindow = {}): Promise<LlmRollup> {
  const qs = new URLSearchParams()
  windowParams(qs, w)
  return toRollup(await apiGet<RollupApi>(`/metrics/llm/rollup?${qs.toString()}`))
}

// ---- Apify (gasto real de scraping social: tabla apify_runs) ------------------------------------

export interface ApifyKpis {
  costUsd: number
  runs: number
  itemsScraped: number
  itemsKept: number
  /** Runs con status != ok (error + timeout) — pudieron cobrar igual. */
  errors: number
  /** Cuentas seguidas distintas con actividad en el rango. */
  accounts: number
  prevCostUsd: number | null
  prevRuns: number | null
}

export interface ApifySourceCost {
  sourceId: number | null
  sourceName: string
  runs: number
  itemsScraped: number
  costUsd: number
}

export interface ApifyAccountCost {
  platform: string
  account: string
  runs: number
  itemsScraped: number
  costUsd: number
}

export interface ApifyPlatformCost {
  platform: string
  runs: number
  itemsScraped: number
  costUsd: number
}

export interface ApifyDailyCost {
  day: string
  total: number
  byPlatform: Record<string, number>
}

export interface ApifyRollup {
  kpis: ApifyKpis
  bySource: ApifySourceCost[]
  byAccount: ApifyAccountCost[]
  byPlatform: ApifyPlatformCost[]
  daily: ApifyDailyCost[]
  platforms: string[]
}

export interface ApifyRunRow {
  id: number
  createdAt: string
  platform: string
  account: string
  actorId: string
  apifyRunId: string | null
  status: string
  itemsScraped: number
  itemsKept: number
  /** null = Apify aún no asentó el costo del run. */
  costUsd: number | null
  chargedEvents: Record<string, number> | null
  sourceId: number | null
  sourceName: string | null
  ingestionRunId: string | null
}

interface ApifyKpisApi {
  cost_usd: number
  runs: number
  items_scraped: number
  items_kept: number
  errors: number
  accounts: number
  prev_cost_usd: number | null
  prev_runs: number | null
}
interface ApifySourceApi { source_id: number | null; source_name: string; runs: number; items_scraped: number; cost_usd: number }
interface ApifyAccountApi { platform: string; account: string; runs: number; items_scraped: number; cost_usd: number }
interface ApifyPlatformApi { platform: string; runs: number; items_scraped: number; cost_usd: number }
interface ApifyDailyApi { day: string; total: number; by_platform: Record<string, number> }
interface ApifyRollupApi {
  kpis: ApifyKpisApi
  by_source: ApifySourceApi[]
  by_account: ApifyAccountApi[]
  by_platform: ApifyPlatformApi[]
  daily: ApifyDailyApi[]
  platforms: string[]
}
interface ApifyRunApi {
  id: number
  created_at: string
  platform: string
  account: string
  actor_id: string
  apify_run_id: string | null
  status: string
  items_scraped: number
  items_kept: number
  cost_usd: number | null
  charged_events: Record<string, number> | null
  source_id: number | null
  source_name: string | null
  ingestion_run_id: string | null
}

function toApifyRollup(r: ApifyRollupApi): ApifyRollup {
  return {
    kpis: {
      costUsd: r.kpis.cost_usd,
      runs: r.kpis.runs,
      itemsScraped: r.kpis.items_scraped,
      itemsKept: r.kpis.items_kept,
      errors: r.kpis.errors,
      accounts: r.kpis.accounts,
      prevCostUsd: r.kpis.prev_cost_usd,
      prevRuns: r.kpis.prev_runs,
    },
    bySource: r.by_source.map((s) => ({
      sourceId: s.source_id,
      sourceName: s.source_name,
      runs: s.runs,
      itemsScraped: s.items_scraped,
      costUsd: s.cost_usd,
    })),
    byAccount: r.by_account.map((a) => ({
      platform: a.platform,
      account: a.account,
      runs: a.runs,
      itemsScraped: a.items_scraped,
      costUsd: a.cost_usd,
    })),
    byPlatform: r.by_platform.map((p) => ({
      platform: p.platform,
      runs: p.runs,
      itemsScraped: p.items_scraped,
      costUsd: p.cost_usd,
    })),
    daily: r.daily.map((d) => ({ day: d.day, total: d.total, byPlatform: d.by_platform })),
    platforms: r.platforms,
  }
}

function toApifyRun(r: ApifyRunApi): ApifyRunRow {
  return {
    id: r.id,
    createdAt: r.created_at,
    platform: r.platform,
    account: r.account,
    actorId: r.actor_id,
    apifyRunId: r.apify_run_id,
    status: r.status,
    itemsScraped: r.items_scraped,
    itemsKept: r.items_kept,
    costUsd: r.cost_usd,
    chargedEvents: r.charged_events,
    sourceId: r.source_id,
    sourceName: r.source_name,
    ingestionRunId: r.ingestion_run_id,
  }
}

/** Rollup del gasto Apify del rango (KPIs + por fuente/cuenta/plataforma + serie diaria). */
export async function fetchApifyRollup(w: MetricsWindow = {}): Promise<ApifyRollup> {
  const qs = new URLSearchParams()
  windowParams(qs, w)
  return toApifyRollup(await apiGet<ApifyRollupApi>(`/metrics/apify/rollup?${qs.toString()}`))
}

/** Corridas de actor crudas (auditoría), más recientes primero. */
export async function fetchApifyRuns(
  w: MetricsWindow = {},
  opts: { limit?: number; offset?: number } = {},
): Promise<{ items: ApifyRunRow[]; total: number }> {
  const qs = new URLSearchParams()
  windowParams(qs, w)
  qs.set("limit", String(opts.limit ?? 50))
  qs.set("offset", String(opts.offset ?? 0))
  const r = await apiGet<{ items: ApifyRunApi[]; total: number }>(
    `/metrics/apify/runs?${qs.toString()}`,
  )
  return { items: r.items.map(toApifyRun), total: r.total }
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

export function multiParam(
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
