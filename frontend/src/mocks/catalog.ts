import type { LlmPurpose, ModelPricing, Source, WorkerJob } from "@/types/domain"

// Sources de ingesta reales del dueño (las categorías del contrato Source).
export const SOURCES: Source[] = [
  { id: 1, name: "Correo universitario", type: "imap", enabled: true, createdAt: "2026-05-23T00:00:00Z", config: { folder: "INBOX", host: "outlook.office365.com" }, fetchModes: ["incremental", "range", "last"] },
  { id: 2, name: "Gmail personal", type: "imap", enabled: true, createdAt: "2026-05-23T00:00:00Z", config: { folder: "INBOX", host: "imap.gmail.com" }, fetchModes: ["incremental", "range", "last"] },
  { id: 3, name: "Telegram · personal", type: "telegram", enabled: true, createdAt: "2026-05-24T00:00:00Z", config: { mode: "polling" }, fetchModes: ["incremental"] },
  { id: 4, name: "Telegram · canales", type: "telegram", enabled: true, createdAt: "2026-05-24T00:00:00Z", config: { mode: "streaming" }, fetchModes: ["incremental"] },
  { id: 5, name: "Instagram", type: "social", enabled: true, createdAt: "2026-05-26T00:00:00Z", config: { platform: "instagram" }, fetchModes: ["incremental"] },
  { id: 6, name: "Facebook", type: "social", enabled: false, createdAt: "2026-05-26T00:00:00Z", config: { platform: "facebook" }, fetchModes: ["incremental"] },
]

export const SOURCE_BY_ID: Record<number, Source> = Object.fromEntries(
  SOURCES.map((s) => [s.id, s]),
)

// Mapa id→nombre de las sources que ingieren (para selects y columnas).
export const INGESTING_LABEL: Record<number, string> = Object.fromEntries(
  SOURCES.filter((s) => s.type !== "calendar").map((s) => [s.id, s.name]),
)

// Precios LLM por 1M tokens — calca MODEL_PRICING de memex/llm/pricing.py.
export const MODEL_PRICING: Record<string, ModelPricing> = {
  "deepseek-v4-flash": { label: "Flash", cacheHit: 0.14, cacheMiss: 0.28, output: 0.28 },
  "deepseek-v4-pro": { label: "Pro", cacheHit: 0.435, cacheMiss: 1.74, output: 3.48 },
  "vision-ocr-1": { label: "OCR (visión)", cacheHit: 0.2, cacheMiss: 0.55, output: 1.6 },
  // Modelo NO tabulado → compute_cost devuelve 0 silencioso. Bug a señalar en la UI.
  "deepseek-v4-flash-preview": { label: "Flash (preview)", cacheHit: 0, cacheMiss: 0, output: 0, untabulated: true },
}

export const PURPOSES: { key: LlmPurpose; label: string; chart: string }[] = [
  { key: "summarize", label: "Resumen", chart: "var(--chart-1)" },
  { key: "extract", label: "Extracción", chart: "var(--chart-2)" },
  { key: "calendar_dedup", label: "Dedup calendar", chart: "var(--chart-3)" },
  { key: "calendar_merge", label: "Merge calendar", chart: "var(--chart-4)" },
  { key: "ocr", label: "OCR", chart: "var(--chart-5)" },
]

export const PURPOSE_LABEL: Record<LlmPurpose, string> = Object.fromEntries(
  PURPOSES.map((p) => [p.key, p.label]),
) as Record<LlmPurpose, string>

export const PURPOSE_CHART: Record<LlmPurpose, string> = Object.fromEntries(
  PURPOSES.map((p) => [p.key, p.chart]),
) as Record<LlmPurpose, string>

export const JOBS: { key: WorkerJob; label: string }[] = [
  { key: "classify", label: "Clasificar" },
  { key: "summarize", label: "Resumir" },
  { key: "extract", label: "Extraer" },
  { key: "calendar", label: "Calendario" },
  { key: "ocr", label: "OCR" },
]

export const JOB_LABEL: Record<WorkerJob, string> = Object.fromEntries(
  JOBS.map((j) => [j.key, j.label]),
) as Record<WorkerJob, string>

export const TIER_LABEL = {
  blacklist: "Blacklist",
  batch: "Batch",
  individual: "Individual",
} as const
