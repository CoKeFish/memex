// Selectores puros sobre los datasets mock. Toda agregación que las vistas necesitan
// vive acá (no en los componentes), parametrizada por rango temporal.

import {
  NOW,
  ingestionRuns,
  inbox,
  llmCalls,
  reviewItems,
  workerRuns,
} from "@/mocks"
import { INGESTING_LABEL, SOURCES } from "@/mocks/catalog"
import { MODEL_PRICING, PURPOSES } from "@/mocks/catalog"
import { ageBucket, type AgeBucket } from "@/lib/format"
import type {
  IngestionRun,
  LlmCall,
  LlmPurpose,
  Source,
  WorkerJob,
  WorkerRun,
} from "@/types/domain"

const DAY = 86_400_000
const MIN = 60_000

export type RangeKey = "24h" | "7d" | "30d" | "90d" | "all"

export const RANGES: { key: RangeKey; label: string; short: string }[] = [
  { key: "24h", label: "Últimas 24 h", short: "24h" },
  { key: "7d", label: "Últimos 7 días", short: "7d" },
  { key: "30d", label: "Últimos 30 días", short: "30d" },
  { key: "90d", label: "Últimos 90 días", short: "90d" },
  { key: "all", label: "Todo", short: "Todo" },
]

export function rangeMs(key: RangeKey): number {
  switch (key) {
    case "24h":
      return DAY
    case "7d":
      return 7 * DAY
    case "30d":
      return 30 * DAY
    case "90d":
      return 90 * DAY
    case "all":
      return Number.POSITIVE_INFINITY
  }
}

function within(iso: string, fromMs: number, toMs: number): boolean {
  const t = new Date(iso).getTime()
  return t >= fromMs && t < toMs
}

// ---- Costo LLM ----------------------------------------------------------------

export interface CostKpis {
  cost: number
  calls: number
  tokens: number
  avgCost: number
  deltaPct: number | null // vs periodo anterior de igual longitud
}

export function costKpis(range: RangeKey, now: Date = NOW): CostKpis {
  const span = rangeMs(range)
  const end = now.getTime()
  const start = span === Infinity ? 0 : end - span
  const inWin = llmCalls.filter((c) => within(c.createdAt, start, end))
  const cost = inWin.reduce((a, c) => a + c.costUsd, 0)
  const tokens = inWin.reduce((a, c) => a + c.promptTokens + c.completionTokens, 0)
  let deltaPct: number | null = null
  if (span !== Infinity) {
    const prev = llmCalls
      .filter((c) => within(c.createdAt, start - span, start))
      .reduce((a, c) => a + c.costUsd, 0)
    deltaPct = prev > 0 ? (cost - prev) / prev : null
  }
  return { cost, calls: inWin.length, tokens, avgCost: inWin.length ? cost / inWin.length : 0, deltaPct }
}

export function callsInRange(range: RangeKey, now: Date = NOW): LlmCall[] {
  const span = rangeMs(range)
  const end = now.getTime()
  const start = span === Infinity ? 0 : end - span
  return llmCalls.filter((c) => within(c.createdAt, start, end))
}

export interface PurposeAgg {
  purpose: LlmPurpose
  label: string
  chart: string
  cost: number
  calls: number
  avg: number
}

export function costByPurpose(range: RangeKey, now: Date = NOW): PurposeAgg[] {
  const calls = callsInRange(range, now)
  return PURPOSES.map((p) => {
    const sub = calls.filter((c) => c.purpose === p.key)
    const cost = sub.reduce((a, c) => a + c.costUsd, 0)
    return { purpose: p.key, label: p.label, chart: p.chart, cost, calls: sub.length, avg: sub.length ? cost / sub.length : 0 }
  }).sort((a, b) => b.cost - a.cost)
}

export interface ModelAgg {
  model: string
  label: string
  untabulated: boolean
  cost: number
  calls: number
  promptTokens: number
  completionTokens: number
  costPer1k: number
}

export function costByModel(range: RangeKey, now: Date = NOW): ModelAgg[] {
  const calls = callsInRange(range, now)
  const byModel = new Map<string, LlmCall[]>()
  for (const c of calls) {
    const arr = byModel.get(c.model) ?? []
    arr.push(c)
    byModel.set(c.model, arr)
  }
  return [...byModel.entries()]
    .map(([model, sub]) => {
      const cost = sub.reduce((a, c) => a + c.costUsd, 0)
      const toks = sub.reduce((a, c) => a + c.promptTokens + c.completionTokens, 0)
      return {
        model,
        label: MODEL_PRICING[model]?.label ?? model,
        untabulated: MODEL_PRICING[model]?.untabulated ?? !MODEL_PRICING[model],
        cost,
        calls: sub.length,
        promptTokens: sub.reduce((a, c) => a + c.promptTokens, 0),
        completionTokens: sub.reduce((a, c) => a + c.completionTokens, 0),
        costPer1k: toks ? (cost / toks) * 1000 : 0,
      }
    })
    .sort((a, b) => b.cost - a.cost)
}

export interface DailyPoint {
  date: string // ISO yyyy-mm-dd
  label: string
  total: number
  byPurpose: Record<LlmPurpose, number>
}

export function costDaily(range: RangeKey, now: Date = NOW): DailyPoint[] {
  const span = rangeMs(range)
  const days = span === Infinity ? 30 : Math.min(90, Math.ceil(span / DAY))
  const buckets: DailyPoint[] = []
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now.getTime() - i * DAY)
    const key = d.toISOString().slice(0, 10)
    buckets.push({
      date: key,
      label: new Intl.DateTimeFormat("es-MX", { day: "2-digit", month: "short" }).format(d),
      total: 0,
      byPurpose: { summarize: 0, extract: 0, calendar_dedup: 0, calendar_merge: 0, ocr: 0 },
    })
  }
  const idx = new Map(buckets.map((b, i) => [b.date, i]))
  for (const c of llmCalls) {
    const key = c.createdAt.slice(0, 10)
    const i = idx.get(key)
    if (i === undefined) continue
    buckets[i].byPurpose[c.purpose] += c.costUsd
    buckets[i].total += c.costUsd
  }
  return buckets
}

// ---- Ingesta ------------------------------------------------------------------

export interface RunInvariant extends IngestionRun {
  expected: number
  balanced: boolean
}

/** Agrega el invariante posted = inserted+duplicates+errors+filtered a cada corrida. */
export function ingestionWithInvariant(range: RangeKey, now: Date = NOW): RunInvariant[] {
  const span = rangeMs(range)
  const start = span === Infinity ? 0 : now.getTime() - span
  return ingestionRuns
    .filter((r) => new Date(r.startedAt).getTime() >= start)
    .map((r) => {
      const expected = r.inserted + r.duplicates + r.errors + r.filtered
      return { ...r, expected, balanced: expected === r.posted }
    })
    .sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())
}

export interface IngestionTotals {
  posted: number
  inserted: number
  duplicates: number
  errors: number
  filtered: number
  runs: number
  unbalanced: number
}

export function ingestionTotals(range: RangeKey, now: Date = NOW): IngestionTotals {
  const runs = ingestionWithInvariant(range, now)
  return {
    posted: runs.reduce((a, r) => a + r.posted, 0),
    inserted: runs.reduce((a, r) => a + r.inserted, 0),
    duplicates: runs.reduce((a, r) => a + r.duplicates, 0),
    errors: runs.reduce((a, r) => a + r.errors, 0),
    filtered: runs.reduce((a, r) => a + r.filtered, 0),
    runs: runs.length,
    unbalanced: runs.filter((r) => !r.balanced).length,
  }
}

// ---- Salud de sources ---------------------------------------------------------

export interface SourceHealth {
  source: Source
  lastRun: IngestionRun | null
  lastRunAge: AgeBucket | null
  runs: IngestionRun[]
  successRate: number
  totalInserted: number
  totalFiltered: number
}

export function sourceHealth(now: Date = NOW): SourceHealth[] {
  return SOURCES.filter((s) => s.type !== "calendar").map((source) => {
    const runs = ingestionRuns
      .filter((r) => r.sourceId === source.id)
      .sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())
    const finished = runs.filter((r) => r.status !== "running")
    const ok = finished.filter((r) => r.status === "ok").length
    const lastRun = runs[0] ?? null
    return {
      source,
      lastRun,
      lastRunAge: lastRun ? ageBucket(lastRun.startedAt, now) : null,
      runs,
      successRate: finished.length ? ok / finished.length : 1,
      totalInserted: runs.reduce((a, r) => a + r.inserted, 0),
      totalFiltered: runs.reduce((a, r) => a + r.filtered, 0),
    }
  })
}

// ---- Workers ------------------------------------------------------------------

const STALE_MS = 30 * MIN

export interface WorkerLatest {
  job: WorkerJob
  latest: WorkerRun | null
  isStale: boolean
  runs: WorkerRun[]
}

export function workerLatest(now: Date = NOW): WorkerLatest[] {
  const jobs: WorkerJob[] = ["classify", "summarize", "extract", "calendar", "ocr"]
  return jobs.map((job) => {
    const runs = workerRuns
      .filter((r) => r.job === job)
      .sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime())
    const latest = runs[0] ?? null
    const isStale =
      !!latest && latest.status === "running" && now.getTime() - new Date(latest.startedAt).getTime() > STALE_MS
    return { job, latest, isStale, runs }
  })
}

export function staleWorkerCount(now: Date = NOW): number {
  return workerLatest(now).filter((w) => w.isStale).length
}

// ---- Inbox / revisión globales ------------------------------------------------

export function reviewCount(): number {
  return reviewItems.length
}

export function inboxPendingCount(): number {
  return inbox.filter((r) => r.processedAt === null && !r.processError).length
}

export function inboxErrorCount(): number {
  return inbox.filter((r) => r.processError !== null).length
}

// Label de source para selects/columnas (evita import directo del catálogo en vistas).
export { INGESTING_LABEL }
