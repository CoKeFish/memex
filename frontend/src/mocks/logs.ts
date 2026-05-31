import { Rng } from "@/lib/rng"
import type { LogEvent, LogLevel, ObsTimelineEntry } from "@/types/domain"
import { ingestionRuns, inbox, llmCalls, NOW, reviewItems, workerRuns } from "./index"
import { SOURCE_BY_ID } from "./catalog"

const MIN = 60_000

interface Template {
  event: string
  module: string
  level: LogLevel
  weight: number
  needsInbox?: boolean
  needsSource?: boolean
  needsRun?: boolean
}

// Subconjunto representativo del inventario real de 83 eventos structlog.
const TEMPLATES: Template[] = [
  { event: "http.request", module: "api", level: "info", weight: 14 },
  { event: "http.request.error", module: "api", level: "error", weight: 1 },
  { event: "auth.rejected", module: "api", level: "warning", weight: 1 },
  { event: "gateway.ingest.received", module: "gateway", level: "info", weight: 5, needsSource: true },
  { event: "gateway.ingest.committed", module: "gateway", level: "info", weight: 5, needsSource: true },
  { event: "ingest.committed", module: "ingest", level: "info", weight: 3, needsSource: true },
  { event: "persist.inserted", module: "persist", level: "info", weight: 10, needsInbox: true, needsSource: true },
  { event: "persist.dedupe_conflict", module: "persist", level: "info", weight: 3, needsSource: true },
  { event: "ingestor.run.start", module: "ingestor", level: "info", weight: 3, needsRun: true, needsSource: true },
  { event: "ingestor.run.end", module: "ingestor", level: "info", weight: 3, needsRun: true, needsSource: true },
  { event: "ingestor.run.fatal", module: "ingestor", level: "error", weight: 1, needsRun: true, needsSource: true },
  { event: "imap_login_ok", module: "imap", level: "info", weight: 2 },
  { event: "folder_fetch_end", module: "imap", level: "info", weight: 2, needsSource: true },
  { event: "streaming_runner.source_catchup_done", module: "streaming", level: "info", weight: 2, needsSource: true },
  { event: "streaming_runner.source_reconnect", module: "streaming", level: "warning", weight: 2, needsSource: true },
  { event: "streaming_runner.source_dead_letter", module: "streaming", level: "error", weight: 1, needsSource: true },
  { event: "telegram.fetch.start", module: "telegram", level: "info", weight: 2, needsSource: true },
  { event: "social.fetch.start", module: "social", level: "info", weight: 2, needsSource: true },
  { event: "classifier.run.end", module: "classifier", level: "info", weight: 3 },
  { event: "summarizer.run.empty", module: "summarizer", level: "info", weight: 2 },
  { event: "summarizer.cli.quota_abort", module: "summarizer", level: "error", weight: 1 },
  { event: "extract.item.invalid", module: "extract", level: "warning", weight: 2, needsInbox: true },
  { event: "extract.attribution_miss", module: "extract", level: "warning", weight: 1, needsInbox: true },
  { event: "extract.run.aborted_no_quota", module: "extract", level: "error", weight: 1 },
  { event: "route.decision", module: "route", level: "info", weight: 6, needsInbox: true, needsSource: true },
  { event: "route.dropped", module: "route", level: "info", weight: 4, needsInbox: true, needsSource: true },
  { event: "ocr.run.start", module: "ocr", level: "info", weight: 2 },
  { event: "calendar.dedup.marked", module: "calendar", level: "info", weight: 2 },
  { event: "calendar.push.start", module: "calendar", level: "info", weight: 1 },
  { event: "calendar.sync.token_expired_full_resync", module: "calendar", level: "warning", weight: 1 },
  { event: "scheduler.job.start", module: "scheduler", level: "info", weight: 3 },
  { event: "scheduler.job.end", module: "scheduler", level: "info", weight: 3 },
  { event: "llm.call", module: "llm", level: "info", weight: 9, needsInbox: true },
  { event: "storage.bucket.created", module: "storage", level: "info", weight: 1 },
]

const rng = new Rng(770077)

function hex(n: number): string {
  let s = ""
  for (let i = 0; i < n; i++) s += "0123456789abcdef"[rng.int(0, 15)]
  return s
}

function buildFields(t: Template, sourceId: number | null, inboxId: number | null): Record<string, unknown> {
  const f: Record<string, unknown> = {}
  if (sourceId) f.source = SOURCE_BY_ID[sourceId]?.name ?? sourceId
  switch (t.event) {
    case "http.request":
      f.method = rng.pick(["GET", "POST", "PUT"])
      f.path = rng.pick(["/inbox", "/sources", "/inbox/stats", "/gateway/plugins/correo-uni/ingest"])
      f.status_code = 200
      f.duration_ms = rng.int(4, 180)
      break
    case "http.request.error":
      f.method = "POST"
      f.path = "/ingest/batch"
      f.exc_type = "RequestValidationError"
      break
    case "auth.rejected":
      f.reason = rng.pick(["missing_bearer", "invalid_token"])
      break
    case "persist.inserted":
      f.inbox_id = inboxId
      break
    case "persist.dedupe_conflict":
      f.external_id = `uid:${rng.int(10000, 12000)}`
      break
    case "llm.call":
      f.purpose = rng.pick(["summarize", "extract", "calendar_dedup", "ocr"])
      f.model = rng.pick(["deepseek-v4-flash", "deepseek-v4-pro"])
      f.cost_usd = Number(rng.float(0.0005, 0.02).toFixed(6))
      f.latency_ms = rng.int(400, 3200)
      f.inbox_id = inboxId
      break
    case "route.decision":
      f.chosen = rng.pick(["finance", "calendar", "finance,calendar"])
      f.inbox_id = inboxId
      break
    case "route.dropped":
      f.dropped = "finance"
      f.inbox_id = inboxId
      break
    case "ingestor.run.end":
      f.posted = rng.int(0, 40)
      f.inserted = rng.int(0, 30)
      f.duplicates = rng.int(0, 12)
      break
    case "streaming_runner.source_reconnect":
      f.retry = rng.int(1, 4)
      f.backoff_s = rng.pick([2, 4, 8, 16])
      break
    case "classifier.run.end":
      f.scanned = rng.int(50, 900)
      f.classified = rng.int(50, 900)
      break
    default:
      break
  }
  return f
}

export const logEvents: LogEvent[] = (() => {
  const out: LogEvent[] = []
  let tMs = 2 * MIN
  let reqId = hex(16)
  let reqLeft = rng.int(2, 7)
  for (let i = 0; i < 260; i++) {
    if (reqLeft <= 0) {
      reqId = hex(16)
      reqLeft = rng.int(2, 7)
    }
    reqLeft--
    const t = rng.weighted(TEMPLATES, TEMPLATES.map((x) => x.weight))
    tMs += rng.int(200, 90_000)
    const sourceId = t.needsSource ? rng.int(1, 6) : null
    const inboxId = t.needsInbox ? rng.int(1, inbox.length) : null
    out.push({
      id: `log-${i}`,
      ts: new Date(NOW.getTime() - tMs).toISOString(),
      level: t.level,
      event: t.event,
      module: t.module,
      requestId: t.module === "scheduler" || t.module === "ingestor" ? null : reqId,
      userId: 1,
      runId: t.needsRun ? hex(8) : null,
      sourceId,
      inboxId,
      fields: buildFields(t, sourceId, inboxId),
    })
  }
  return out.sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
})()

// Timeline derivado de lo PERSISTIDO (observabilidad), no de logs efímeros.
export function buildObsTimeline(): ObsTimelineEntry[] {
  const entries: ObsTimelineEntry[] = []
  for (const r of ingestionRuns.slice(0, 20)) {
    entries.push({
      id: `obs-ing-${r.id}`,
      ts: r.startedAt,
      kind: "ingestion",
      title: `Ingesta · ${SOURCE_BY_ID[r.sourceId]?.name ?? r.sourceId}`,
      detail: `posted ${r.posted} · inserted ${r.inserted} · dup ${r.duplicates} · err ${r.errors} · filt ${r.filtered}`,
      tone: r.status === "ok" ? "ok" : r.status === "running" ? "running" : "error",
      requestId: null,
    })
  }
  for (const w of workerRuns) {
    entries.push({
      id: `obs-wrk-${w.id}`,
      ts: w.startedAt,
      kind: "worker",
      title: `Worker · ${w.job}`,
      detail: w.error ?? `status ${w.status}`,
      tone: w.status === "ok" ? "ok" : w.status === "running" ? "running" : "error",
      requestId: null,
    })
  }
  for (const c of llmCalls.slice(0, 30)) {
    entries.push({
      id: `obs-llm-${c.id}`,
      ts: c.createdAt,
      kind: "llm",
      title: `LLM · ${c.purpose} · ${c.model}`,
      detail: `${c.status} · ${c.latencyMs}ms · $${c.costUsd.toFixed(4)}`,
      tone: c.status === "ok" ? "neutral" : "error",
      requestId: c.requestId,
    })
  }
  for (const it of reviewItems) {
    if (!it.deadLetter) continue
    entries.push({
      id: `obs-dl-${it.id}`,
      ts: it.deadLetter.updatedAt,
      kind: "failure",
      title: `Dead-letter · ${it.deadLetter.stage} · inbox #${it.deadLetter.inboxId}`,
      detail: it.deadLetter.lastError ?? "",
      tone: "review",
      requestId: null,
    })
  }
  return entries.sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
}
