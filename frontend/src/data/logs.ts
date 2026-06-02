// Logs del sistema contra la API real. structlog NO se persiste, así que el "stream" de eventos se
// RECONSTRUYE de la tabla `llm_calls` (cada llamada = un evento), igual que la traza de un mensaje.
// El timeline de "observabilidad" se compone de lo persistido por el pipeline: ingestion_runs +
// worker_runs (vía /stats/pipeline). Ambos soportan acotar por módulo donde tiene sentido.

import { fetchLlmCalls, type LlmCallRow, type MetricsWindow } from "./metrics"
import { fetchPipeline } from "./pipeline"
import type { LogEvent, LogLevel, ObsTimelineEntry } from "@/types/domain"

export interface LogsQuery extends MetricsWindow {
  /** Acota a un módulo (derivado de `purpose`: finance, calendar, summarize, routing, ocr, …). */
  module?: string
  limit?: number
}

/** status de una llamada LLM → nivel de log. */
function levelOf(status: string): LogLevel {
  if (status === "error") return "error"
  if (status === "filtered") return "warning"
  return "info"
}

/** Una fila de `llm_calls` → un evento de log (el "stream" reconstruido). */
function callToEvent(c: LlmCallRow): LogEvent {
  const meta = c.metadata ?? {}
  return {
    id: `llm-${c.id}`,
    ts: c.createdAt,
    level: levelOf(c.status),
    event: c.errorMessage ? `${c.purpose}: ${c.errorMessage}` : c.purpose,
    module: c.module,
    requestId: null, // /metrics/llm/calls no devuelve request_id por fila
    userId: null,
    runId: null,
    sourceId: c.sourceId,
    inboxId: c.inboxId,
    fields: { model: c.model, cost_usd: c.costUsd, latency_ms: c.latencyMs, ...meta },
  }
}

/** Stream de eventos reconstruido de `llm_calls`, más nuevos primero, opcionalmente por módulo. */
export async function fetchLogEvents(query: LogsQuery = {}): Promise<LogEvent[]> {
  const { module, limit, ...win } = query
  const { items } = await fetchLlmCalls({
    ...win,
    module: module ? [module] : undefined,
    sort: "created_at",
    dir: "desc",
    limit: limit ?? 100,
  })
  return items.map(callToEvent)
}

/** Timeline de observabilidad persistida (ingestion_runs + worker_runs), más nuevos primero. */
export async function fetchObsTimeline(): Promise<ObsTimelineEntry[]> {
  const p = await fetchPipeline()
  const entries: ObsTimelineEntry[] = []
  for (const r of p.ingestion.runs) {
    entries.push({
      id: `obs-ing-${r.id}`,
      ts: r.startedAt,
      kind: "ingestion",
      title: `Ingesta · ${r.sourceName ?? r.sourceId}`,
      detail: `posted ${r.posted} · inserted ${r.inserted} · dup ${r.duplicates} · err ${r.errors} · filt ${r.filtered}`,
      tone: r.status === "ok" ? "ok" : r.status === "running" ? "running" : "error",
      requestId: null,
    })
  }
  for (const w of p.workers) {
    if (!w.latest) continue
    entries.push({
      id: `obs-wrk-${w.job}`,
      ts: w.latest.startedAt,
      kind: "worker",
      title: `Worker · ${w.job}`,
      detail: w.latest.error ?? `status ${w.latest.status}`,
      tone: w.latest.status === "ok" ? "ok" : w.latest.status === "running" ? "running" : "error",
      requestId: null,
    })
  }
  return entries.sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
}
