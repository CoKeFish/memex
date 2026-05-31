import type { FetchPreview, ModuleSetting, RunPreview, SchedulerJob, WorkerJob } from "@/types/domain"
import { NOW } from "./index"

const MIN = 60_000
const HOUR = 3_600_000
const iso = (msAgo: number): string => new Date(NOW.getTime() - msAgo).toISOString()

/** Off por defecto: nada procesa solo (el daemon se arranca a mano). */
export const schedulerEnabled = false

export const schedulerJobs: SchedulerJob[] = [
  { job: "classify", enabled: true, cron: "*/15 * * * *", lastRun: iso(28 * MIN), nextRun: iso(-2 * MIN) },
  { job: "summarize", enabled: true, cron: "30 * * * *", lastRun: iso(47 * MIN), nextRun: iso(-13 * MIN) },
  { job: "extract", enabled: true, cron: "0 */2 * * *", lastRun: iso(2 * HOUR), nextRun: iso(-58 * MIN) },
  { job: "calendar", enabled: false, cron: "0 */6 * * *", lastRun: iso(22 * MIN), nextRun: null },
  { job: "ocr", enabled: true, cron: "*/30 * * * *", lastRun: iso(29 * MIN), nextRun: iso(-1 * MIN) },
]

export const moduleSettings: ModuleSetting[] = [
  { slug: "finance", label: "Finanzas (gastos)", enabled: true, batchingPolicy: "per_module", groupSize: 3, processed: 870, total: 1000 },
  { slug: "calendar", label: "Calendario (eventos)", enabled: true, batchingPolicy: "grouped", groupSize: 3, processed: 540, total: 1000 },
]

/** Dry-run de fetch: la mayoría ya existe (idempotencia por UNIQUE(source_id, external_id) + checkpoint). */
export function dryRunFetch(sourceId: number, mode: "incremental" | "range" | "last", n = 50): FetchPreview {
  if (mode === "incremental") {
    const nuevos = 4 + ((sourceId * 3) % 12)
    return { scanned: nuevos + 3, nuevos, duplicados: 2 + (sourceId % 2), filtrados: 1 }
  }
  if (mode === "last") {
    const nuevos = Math.min(n, 6 + (sourceId % 5))
    return { scanned: n, nuevos, duplicados: Math.max(0, n - nuevos - 1), filtrados: 1 }
  }
  const scanned = 180 + sourceId * 20
  const nuevos = 5 + (sourceId % 8)
  return { scanned, nuevos, duplicados: scanned - nuevos - 3, filtrados: 3 }
}

const RUN: Record<WorkerJob, RunPreview> = {
  classify: {
    job: "classify",
    pending: 224,
    estimate: [
      { label: "a escanear", value: "224 sin clasificar" },
      { label: "by_tier (est.)", value: "blacklist 20 · batch 190 · individual 14" },
      { label: "costo", value: "US$0 (sin LLM)" },
    ],
    command: "memex-classify run --user 1",
  },
  summarize: {
    job: "summarize",
    pending: 180,
    estimate: [
      { label: "ventanas", value: "~12" },
      { label: "llamadas LLM", value: "~14" },
      { label: "costo est.", value: "US$0.03" },
    ],
    command: "memex-summarize run --tier batch --limit 200",
  },
  extract: {
    job: "extract",
    pending: 96,
    estimate: [
      { label: "ruteo", value: "~40 mensajes" },
      { label: "llamadas", value: "~18" },
      { label: "costo est.", value: "US$0.02" },
    ],
    command: "memex-extract run --batching-policy grouped --group-size 3",
  },
  calendar: {
    job: "calendar",
    pending: 0,
    estimate: [
      { label: "pasos", value: "pull · dedup · consolidate · merge · push" },
      { label: "proveedor", value: "Google" },
    ],
    command: "memex-calendar-sync pull && memex-calendar-sync consolidate",
  },
  ocr: {
    job: "ocr",
    pending: 18,
    estimate: [
      { label: "imágenes pendientes", value: "18" },
      { label: "costo est.", value: "US$0.28" },
    ],
    command: "memex-ocr run --limit 50",
  },
}

export function dryRunRun(job: WorkerJob): RunPreview {
  return RUN[job]
}
