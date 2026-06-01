// Mapa de estados de dominio → tono visual (clases de los tokens --status-* / --origin-*).
import type {
  CalendarOrigin,
  IngestionRunStatus,
  LlmStatus,
  Tier,
  WorkerRunStatus,
} from "@/types/domain"

export type Tone =
  | "ok"
  | "error"
  | "running"
  | "filtered"
  | "review"
  | "pending"
  | "neutral"

export const toneText: Record<Tone, string> = {
  ok: "text-status-ok",
  error: "text-status-error",
  running: "text-status-running",
  filtered: "text-status-filtered",
  review: "text-status-review",
  pending: "text-status-pending",
  neutral: "text-muted-foreground",
}

export function ingestionTone(s: IngestionRunStatus): Tone {
  if (s === "ok") return "ok"
  if (s === "running") return "running"
  return "error" // failed | aborted
}

export function ingestionLabel(s: IngestionRunStatus): string {
  return { ok: "OK", running: "En curso", failed: "Falló", aborted: "Abortada" }[s]
}

export function workerTone(s: WorkerRunStatus): Tone {
  if (s === "ok") return "ok"
  if (s === "running") return "running"
  return "error"
}

export function workerLabel(s: WorkerRunStatus): string {
  return { ok: "OK", running: "En curso", error: "Error" }[s]
}

export function llmTone(s: LlmStatus): Tone {
  if (s === "ok") return "ok"
  if (s === "filtered") return "filtered"
  return "error"
}

export const tierTone: Record<Tier, Tone> = {
  blacklist: "neutral",
  batch: "running",
  individual: "review",
}

export const tierLabel: Record<Tier, string> = {
  blacklist: "Lista negra",
  batch: "Lote",
  individual: "Individual",
}

/**
 * Estado de procesamiento de un inbox, derivado del avance REAL del pipeline (clasificación →
 * resumen/extracción). No usamos `inbox.processed_at` porque quedó en desuso (casi nunca se setea).
 */
export function inboxStatus(row: {
  processError?: string | null
  classification?: { tier: string } | null
  summarized?: boolean
  extracted?: boolean
}): { tone: Tone; label: string } {
  if (row.processError) return { tone: "error", label: "Error al procesar" }
  const tier = row.classification?.tier
  if (!tier) return { tone: "pending", label: "Sin clasificar" }
  if (tier === "blacklist") return { tone: "filtered", label: "Ignorado (lista negra)" }
  if (row.summarized || row.extracted) return { tone: "ok", label: "Procesado" }
  return { tone: "review", label: "Clasificado · sin procesar" }
}

// Origen del evento de calendar → token de origin.
export const originText: Record<CalendarOrigin, string> = {
  extraction: "text-origin-inbox",
  provider: "text-origin-provider",
  module: "text-origin-module",
}

export const originLabel: Record<CalendarOrigin, string> = {
  extraction: "Extracción",
  provider: "Proveedor",
  module: "Módulo",
}

/** Color (CSS var) por origen — para chips/eventos de calendario. */
export const originChart: Record<CalendarOrigin, string> = {
  extraction: "var(--origin-inbox)",
  provider: "var(--origin-provider)",
  module: "var(--origin-module)",
}

// Semáforo de frescura → tono.
export const freshnessTone: Record<"fresh" | "warn" | "stale", Tone> = {
  fresh: "ok",
  warn: "review",
  stale: "error",
}
