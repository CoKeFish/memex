// Catálogo de módulos para la vista /metricas. El backend deriva el módulo de `llm_calls.purpose`
// (routing, summarize, grouped, <slug de extracción: finance/calendar/…>, ocr). Acá viven solo sus
// etiquetas y colores de gráfico; cualquier módulo no listado (un purpose futuro) cae a un fallback
// neutro, así nunca desaparece del gráfico ni rompe las series apiladas.

export const MODULES: { key: string; label: string; chart: string }[] = [
  { key: "routing", label: "Ruteo", chart: "var(--chart-1)" },
  { key: "summarize", label: "Resumen", chart: "var(--chart-2)" },
  { key: "finance", label: "Extr. finanzas", chart: "var(--chart-3)" },
  { key: "calendar", label: "Calendario", chart: "var(--chart-4)" },
  { key: "grouped", label: "Extr. agrupada", chart: "var(--chart-5)" },
  { key: "ocr", label: "OCR", chart: "var(--origin-inbox)" },
]

const BY_KEY = new Map(MODULES.map((m) => [m.key, m]))
const FALLBACK_CHART = "var(--status-filtered)"

/** Etiqueta legible de un módulo; fallback al propio key para purposes futuros. */
export function moduleLabel(key: string): string {
  return BY_KEY.get(key)?.label ?? key
}

/** Color de gráfico de un módulo; fallback neutro para módulos no catalogados. */
export function moduleChart(key: string): string {
  return BY_KEY.get(key)?.chart ?? FALLBACK_CHART
}
