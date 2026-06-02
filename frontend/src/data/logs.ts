// Logs del sistema contra la API real. AHORA el "stream" de eventos sale de la tabla `log_events`
// (el sink real de structlog, migración 0020) vía GET /logs — ya NO se reconstruye de `llm_calls`.
// Cada evento structlog se persiste con su nivel/logger/correlación reales (request_id, run_id,
// source_id, inbox_id) y sus `fields`. GET /logs/stats agrega el rango (KPIs + histograma + cortes
// por nivel/evento/logger + percentiles de latencia). El timeline de "observabilidad" sigue saliendo
// de lo persistido por el pipeline (ingestion_runs + worker_runs vía /stats/pipeline).

import { apiGet } from "@/lib/api"
import { fetchPipeline } from "./pipeline"
import { multiParam, windowParams, type FilterMode, type MetricsWindow } from "./metrics"
import type {
  LogEventRow,
  LogStats,
  ObsTimelineEntry,
} from "@/types/domain"

// ---- Query (ventana + filtros server-side) ------------------------------------------------------

/** Filtros del stream/stats de logs. Reusa `MetricsWindow` (since/until/tz) y agrega las dimensiones
 *  de `log_events`: nivel/evento/logger con modo incluir/excluir, correlación, búsqueda y paginación.
 *  /logs usa sort/dir/limit/offset; /logs/stats ignora esos cuatro (agrega todo el rango filtrado). */
export interface LogsQuery extends MetricsWindow {
  level?: string[]
  levelMode?: FilterMode
  event?: string[]
  eventMode?: FilterMode
  logger?: string[]
  loggerMode?: FilterMode
  requestId?: string
  runId?: string
  sourceId?: number
  inboxId?: number
  q?: string
  sort?: "ts"
  dir?: "asc" | "desc"
  limit?: number
  offset?: number
}

// ---- API rows (snake_case) + transform ----------------------------------------------------------

interface LogEventApi {
  id: number
  ts: string
  level: string
  event: string
  logger: string | null
  user_id: number | null
  request_id: string | null
  run_id: string | null
  source_id: number | null
  inbox_id: number | null
  exception: string | null
  fields: Record<string, unknown>
}

interface LogLevelCountApi { level: string; count: number }
interface LogEventCountApi { event: string; count: number }
interface LogLoggerCountApi { logger: string; count: number }
interface LogHistogramPointApi { bucket: string; total: number; errors: number }
interface LogLatencyApi { p50: number | null; p95: number | null; p99: number | null }
interface LogStatsApi {
  total: number
  errors: number
  error_rate: number
  by_level: LogLevelCountApi[]
  by_event: LogEventCountApi[]
  by_logger: LogLoggerCountApi[]
  histogram: LogHistogramPointApi[]
  latency: LogLatencyApi
  sink_dropped: number
}

/** Fila cruda de /logs (snake_case) → `LogEventRow` (camelCase). `level` viene como string libre del
 *  backend; el tipo `LogLevel` lo restringe a debug|info|warning|error|critical (se asume válido). */
function toRow(r: LogEventApi): LogEventRow {
  return {
    id: r.id,
    ts: r.ts,
    level: r.level as LogEventRow["level"],
    event: r.event,
    logger: r.logger,
    userId: r.user_id,
    requestId: r.request_id,
    runId: r.run_id,
    sourceId: r.source_id,
    inboxId: r.inbox_id,
    exception: r.exception,
    fields: r.fields,
  }
}

function toStats(r: LogStatsApi): LogStats {
  return {
    total: r.total,
    errors: r.errors,
    errorRate: r.error_rate,
    byLevel: r.by_level.map((l) => ({ level: l.level as LogStats["byLevel"][number]["level"], count: l.count })),
    byEvent: r.by_event.map((e) => ({ event: e.event, count: e.count })),
    byLogger: r.by_logger.map((g) => ({ logger: g.logger, count: g.count })),
    histogram: r.histogram.map((h) => ({ bucket: h.bucket, total: h.total, errors: h.errors })),
    latency: { p50: r.latency.p50, p95: r.latency.p95, p99: r.latency.p99 },
    sinkDropped: r.sink_dropped,
  }
}

/** Filtros comunes a /logs y /logs/stats: ventana + dimensiones incluir/excluir + correlación + q. */
function logParams(qs: URLSearchParams, query: LogsQuery): void {
  windowParams(qs, query)
  multiParam(qs, "level", query.level, query.levelMode)
  multiParam(qs, "event", query.event, query.eventMode)
  multiParam(qs, "logger", query.logger, query.loggerMode)
  if (query.requestId) qs.set("request_id", query.requestId)
  if (query.runId) qs.set("run_id", query.runId)
  if (query.sourceId != null) qs.set("source_id", String(query.sourceId))
  if (query.inboxId != null) qs.set("inbox_id", String(query.inboxId))
  if (query.q) qs.set("q", query.q)
}

// ---- Endpoints ----------------------------------------------------------------------------------

/** Stream de eventos de `log_events` con filtros incluir/excluir, orden y paginación offset. */
export async function fetchLogs(query: LogsQuery): Promise<{ items: LogEventRow[]; total: number }> {
  const qs = new URLSearchParams()
  logParams(qs, query)
  if (query.sort) qs.set("sort", query.sort)
  if (query.dir) qs.set("dir", query.dir)
  qs.set("limit", String(query.limit ?? 100))
  qs.set("offset", String(query.offset ?? 0))
  const r = await apiGet<{ items: LogEventApi[]; total: number }>(`/logs?${qs.toString()}`)
  return { items: r.items.map(toRow), total: r.total }
}

/** Agregaciones del rango filtrado (KPIs + histograma + cortes por nivel/evento/logger + latencia). */
export async function fetchLogStats(query: LogsQuery): Promise<LogStats> {
  const qs = new URLSearchParams()
  logParams(qs, query)
  return toStats(await apiGet<LogStatsApi>(`/logs/stats?${qs.toString()}`))
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
