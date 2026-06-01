// Lógica pura de la traza LLM (sin JSX): agrupa las llamadas en "corridas" (un mismo "Procesar")
// por request_id, con fallback por cercanía temporal para las corridas batch/CLI que no lo traen.
// Vive aparte del componente para no romper Fast Refresh (react-refresh/only-export-components).

import type { InboxLlmCall } from "@/types/domain"

// Dos llamadas sin request_id separadas por más de esto = corridas distintas.
const GAP_MS = 90_000

export type RunPhase = "summarize" | "extract" | "calendar" | "ocr" | "mixed" | "other"

export interface Run {
  key: string
  calls: InboxLlmCall[]
  startedAt: string | null
  startedAtMs: number
  endedAtMs: number
  promptTokens: number
  completionTokens: number
  costUsd: number
  latencyMs: number
  status: "ok" | "error"
  phase: RunPhase
  isLatest: boolean
  producedSummary: boolean
  producedExtraction: boolean
}

function str(v: unknown): string {
  return v == null ? "" : String(v)
}

function tsOf(c: InboxLlmCall): number {
  const t = c.createdAt ? Date.parse(c.createdAt) : NaN
  return Number.isNaN(t) ? 0 : t
}

export function isExtractCall(c: InboxLlmCall): boolean {
  return c.purpose === "module_route" || c.purpose.startsWith("extract")
}

function derivePhase(calls: InboxLlmCall[]): RunPhase {
  const summarize = calls.some((c) => c.purpose.startsWith("summarize"))
  const extract = calls.some(isExtractCall)
  const calendar = calls.some((c) => c.purpose.startsWith("calendar"))
  const ocr = calls.some((c) => c.purpose === "ocr")
  if (summarize && extract) return "mixed"
  if (summarize) return "summarize"
  if (extract) return "extract"
  if (ocr) return "ocr" // corrida del worker memex-ocr (visión + omitidos), sin LLM de texto
  if (calendar) return "calendar"
  return "other"
}

function buildRun(calls: InboxLlmCall[], idx: number): Run {
  const times = calls.map(tsOf)
  const startedAtMs = Math.min(...times)
  const endedAtMs = Math.max(...times)
  const first = calls.find((c) => tsOf(c) === startedAtMs)
  return {
    key: calls.find((c) => c.requestId)?.requestId ?? `batch-${idx}-${startedAtMs}`,
    calls,
    startedAt: first?.createdAt ?? null,
    startedAtMs,
    endedAtMs,
    promptTokens: calls.reduce((a, c) => a + c.promptTokens, 0),
    completionTokens: calls.reduce((a, c) => a + c.completionTokens, 0),
    costUsd: calls.reduce((a, c) => a + c.costUsd, 0),
    latencyMs: calls.reduce((a, c) => a + c.latencyMs, 0),
    status: calls.some((c) => c.status === "error") ? "error" : "ok",
    phase: derivePhase(calls),
    isLatest: false,
    producedSummary: false,
    producedExtraction: false,
  }
}

/** Marca la corrida MÁS RECIENTE con una llamada ok del tipo dado (la que dejó el resultado vigente). */
function markProduced(
  runs: Run[],
  pred: (c: InboxLlmCall) => boolean,
  flag: "producedSummary" | "producedExtraction",
) {
  let best: Run | null = null
  for (const r of runs) {
    if (r.calls.some((c) => c.status === "ok" && pred(c)) && (!best || r.startedAtMs > best.startedAtMs)) {
      best = r
    }
  }
  if (best) best[flag] = true
}

/** Agrupa las llamadas en corridas (función pura). Orden cronológico ascendente. */
export function groupCallsIntoRuns(calls: InboxLlmCall[]): Run[] {
  const sorted = [...calls].sort((a, b) => tsOf(a) - tsOf(b))
  const groups: InboxLlmCall[][] = []
  let prev: InboxLlmCall | null = null
  for (const c of sorted) {
    const sameReq = (c.requestId ?? null) === (prev?.requestId ?? null)
    const bothNull = !c.requestId && !prev?.requestId
    const gapOk = bothNull && prev != null && tsOf(c) - tsOf(prev) <= GAP_MS
    if (!prev || !sameReq || (bothNull && !gapOk)) groups.push([c])
    else groups[groups.length - 1].push(c)
    prev = c
  }
  const runs = groups.map(buildRun)
  const latest = runs.reduce<Run | null>((best, r) => (!best || r.startedAtMs > best.startedAtMs ? r : best), null)
  if (latest) latest.isLatest = true
  markProduced(runs, (c) => c.purpose.startsWith("summarize"), "producedSummary")
  markProduced(runs, isExtractCall, "producedExtraction")
  return runs
}

export function fmtCost(usd: number): string {
  if (!usd) return "$0"
  if (usd < 0.01) return `$${usd.toFixed(6)}`
  return `$${usd.toFixed(4)}`
}

/** Resume la decisión de una llamada desde su metadata (auditoría del ruteo / extracción / OCR). */
export function callDetail(c: InboxLlmCall): string {
  const m = c.metadata ?? {}
  const list = (v: unknown) => (Array.isArray(v) ? v.map(String).join(", ") : str(v))
  if (c.purpose === "module_route") {
    return `evaluó: ${list(m.slugs_in) || "—"} → eligió: ${list(m.chosen) || "ninguno"}`
  }
  if (c.purpose.startsWith("extract")) {
    return `items: ${str(m.items)} · descartados: ${str(m.discarded)}${m.n ? ` · ventana: ${str(m.n)} msj` : ""}`
  }
  if (c.purpose.startsWith("summarize")) {
    return m.n ? `ventana: ${str(m.n)} msj` : ""
  }
  if (c.purpose === "ocr") return ocrDetail(m)
  return ""
}

/** Detalle de una llamada/evento OCR: transcripción de una imagen, omisión por tope, o manifiesto. */
function ocrDetail(m: Record<string, unknown>): string {
  const kind = str(m.kind)
  if (kind === "pdf-skipped" || kind === "zip-pdf-skipped") {
    const tope = m.max_images ? ` (tope ${str(m.max_images)} imgs)` : ""
    const origin = m.origin ? ` · ${str(m.origin)}` : ""
    return `omitido · ${str(m.skipped_reason) || "límite de imágenes"}${tope}${origin}`
  }
  if (kind === "zip-manifest") {
    const entries = Array.isArray(m.entries) ? m.entries.length : 0
    const skipped = Array.isArray(m.skipped) ? m.skipped.length : 0
    const trunc = m.truncated === true ? " · truncado" : ""
    return `ZIP: ${entries} entrada(s)${skipped ? ` · ${skipped} salteada(s)` : ""}${trunc}`
  }
  // Llamada de visión real sobre una imagen / página.
  const label = kind || "imagen"
  const origin = m.origin ? ` · ${str(m.origin)}` : ""
  const chars = m.chars != null ? ` · ${str(m.chars)} chars` : ""
  const trunc = m.truncated === true ? " · truncado" : ""
  return `${label}${origin}${chars}${trunc}`
}
