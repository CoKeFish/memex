import { Rng } from "@/lib/rng"
import type {
  AlertEvent,
  CalendarConflict,
  CalendarDedupCandidate,
  IngestionRun,
  InboxPayload,
  InboxRow,
  LlmCall,
  LlmPurpose,
  ReviewItem,
  Source,
  WorkerJob,
  WorkerRun,
  WorkItemFailure,
} from "@/types/domain"
import { MODEL_PRICING, SOURCES } from "./catalog"

// Ancla temporal: capturada una vez al cargar el módulo. Los timestamps se generan
// como offsets, así los "hace X" y la detección de stale se comparan contra el reloj real.
export const NOW = new Date()
const MIN = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000

function iso(msAgo: number): string {
  return new Date(NOW.getTime() - msAgo).toISOString()
}

const rng = new Rng(20260531)

function costOf(model: string, promptTokens: number, cacheHitTokens: number, output: number): number {
  const p = MODEL_PRICING[model]
  if (!p || p.untabulated) return 0
  const miss = Math.max(0, promptTokens - cacheHitTokens)
  return (
    (cacheHitTokens / 1e6) * p.cacheHit +
    (miss / 1e6) * p.cacheMiss +
    (output / 1e6) * p.output
  )
}

// ---- Inbox --------------------------------------------------------------------

const EMAIL_SENDERS = [
  { email: "no-reply@uaem.mx", name: "Servicios Escolares UAEM" },
  { email: "profesor.garcia@uni.edu", name: "Dr. García" },
  { email: "facturacion@railway.app", name: "Railway" },
  { email: "team@github.com", name: "GitHub" },
  { email: "newsletter@stratechery.com", name: "Stratechery" },
  { email: "ana.lopez@gmail.com", name: "Ana López" },
  { email: "soporte@banco.mx", name: "Banco · Notificaciones" },
  { email: "no-reply@calendar.google.com", name: "Google Calendar" },
]
const EMAIL_SUBJECTS = [
  "Recordatorio: entrega de proyecto final",
  "Tu recibo de Railway — US$2.54",
  "[GitHub] Se abrió un issue en memex",
  "Calificaciones disponibles en el portal",
  "Cargo aprobado: $160.000 ARS",
  "Reunión reprogramada para el jueves",
  "Confirmación de inscripción a la materia",
  "Tu resumen semanal",
]
const EMAIL_BODIES = [
  "Te recordamos que la entrega del proyecto vence el viernes a las 23:59. Sube el PDF al portal.",
  "Gracias por tu pago. Total facturado: US$2.54 por el periodo de mayo.",
  "Un nuevo issue fue abierto: 'dead-letter de mensajes veneno'. Revisa los detalles en el repositorio.",
  "Tu cargo de $160.000 ARS fue aprobado en COMERCIO XYZ el 28/05.",
  "La reunión de seguimiento se movió al jueves 10:00 en el aula 204.",
]
const TG_CHATS = [
  { id: -1001234567890, kind: "supergroup" as const, title: "Hackathon LATAM 2026" },
  { id: -1009876543210, kind: "channel" as const, title: "Ofertas Dev · Remoto" },
  { id: -1005550001111, kind: "supergroup" as const, title: "Universidad · Avisos" },
  { id: -1002223334445, kind: "group" as const, title: "Familia" },
]
const TG_SENDERS = ["Roy", "Mariana", "Coordinación", "Bot Avisos", "Carlos M."]
const TG_TEXTS = [
  "El registro al hackathon cierra el 5 de junio, no olviden inscribirse.",
  "Nueva vacante: Backend Python remoto, USD según experiencia. DM para postular.",
  "Mañana no hay clase de Sistemas, se repone el sábado 9:00.",
  "¿Alguien tiene los apuntes del parcial?",
  "Recordatorio: cena familiar el domingo a las 14h.",
]
const SOCIAL_ACCOUNTS = [
  { platform: "instagram" as const, account: "uaem.oficial", name: "UAEM Oficial" },
  { platform: "instagram" as const, account: "devjobs.latam", name: "Dev Jobs LATAM" },
  { platform: "facebook" as const, account: "ComunidadPython", name: "Comunidad Python" },
]
const SOCIAL_TEXTS = [
  "📢 Abrimos convocatoria para el hackathon de fin de semestre. Premios y mentorías.",
  "Nueva oferta laboral: desarrollador full-stack. Link en bio.",
  "Charla gratuita sobre observabilidad este jueves 19h. Cupos limitados.",
]

const PROCESS_ERRORS = [
  "json.decoder.JSONDecodeError: Expecting value: line 1 column 1",
  "ValueError: payload sin campo 'date'",
  "httpx.ReadTimeout: timed out al renderizar adjunto",
]

function buildPayload(source: Source, i: number): InboxPayload {
  if (source.type === "imap") {
    const s = rng.pick(EMAIL_SENDERS)
    const isBulk = rng.bool(0.45)
    return {
      from: s,
      subject: rng.pick(EMAIL_SUBJECTS),
      date: iso(rng.skewed(0, 30 * DAY)),
      body_text: rng.pick(EMAIL_BODIES),
      folder: "INBOX",
      list_id: isBulk ? "<newsletter.list.id>" : null,
      list_unsubscribe: isBulk ? "<mailto:unsub@x.com>" : null,
      precedence: isBulk ? "bulk" : null,
      attachments: rng.bool(0.2)
        ? [{ filename: "recibo.pdf", content_type: "application/pdf", size: rng.int(20_000, 300_000) }]
        : [],
    }
  }
  if (source.type === "telegram") {
    const c = rng.pick(TG_CHATS)
    return {
      chat_id: c.id,
      chat_kind: c.kind,
      chat_title: c.title,
      message_id: 1000 + i,
      sender: { user_id: rng.int(1, 9999), display_name: rng.pick(TG_SENDERS) },
      date: iso(rng.skewed(0, 30 * DAY)),
      text: rng.pick(TG_TEXTS),
      media_kind: rng.weighted(["photo", "sticker", "document", "none"] as const, [26, 8, 6, 60]),
    }
  }
  const a = rng.pick(SOCIAL_ACCOUNTS)
  return {
    platform: a.platform,
    account: a.account,
    account_name: a.name,
    post_id: `${a.platform}_${100000 + i}`,
    url: `https://${a.platform}.com/p/${100000 + i}`,
    text: rng.pick(SOCIAL_TEXTS),
    posted_at: iso(rng.skewed(0, 30 * DAY)),
    media_kind: rng.pick(["image", "carousel", "video", "none"]),
  }
}

const INGESTING = SOURCES.filter((s) => s.type !== "calendar")

export const inbox: InboxRow[] = Array.from({ length: 2000 }, (_, idx) => {
  const id = idx + 1
  const source = rng.weighted(INGESTING, [5, 5, 3, 3, 2, 1])
  const occurredMsAgo = rng.skewed(MIN, 30 * DAY)
  const occurredAt = iso(occurredMsAgo)
  const receivedAt = iso(Math.max(0, occurredMsAgo - rng.int(MIN, 20 * MIN)))
  const roll = rng.float(0, 1)
  let processedAt: string | null = iso(Math.max(0, occurredMsAgo - rng.int(MIN, 2 * HOUR)))
  let processError: string | null = null
  let attempts = 0
  if (roll < 0.12) {
    processedAt = null // pendiente
  } else if (roll < 0.15) {
    processedAt = null
    processError = rng.pick(PROCESS_ERRORS)
    attempts = rng.int(1, 3)
  }
  return {
    id,
    sourceId: source.id,
    externalId:
      source.type === "imap"
        ? `uid:${10000 + id}`
        : source.type === "telegram"
          ? `${1000 + id}`
          : `${100000 + id}`,
    occurredAt,
    receivedAt,
    payload: buildPayload(source, id),
    processedAt,
    processError,
    attempts,
  }
})

// ---- llm_calls ----------------------------------------------------------------

const PURPOSE_MODELS: Record<LlmPurpose, string[]> = {
  summarize: ["deepseek-v4-flash", "deepseek-v4-flash", "deepseek-v4-pro"],
  extract: ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-pro"],
  calendar_dedup: ["deepseek-v4-flash"],
  calendar_merge: ["deepseek-v4-flash", "deepseek-v4-pro"],
  ocr: ["vision-ocr-1"],
}
const PURPOSES_W: LlmPurpose[] = ["summarize", "extract", "calendar_dedup", "calendar_merge", "ocr"]
const ERR_MSGS = [
  "openai.APIStatusError: 400 content_filter",
  "httpx.ReadTimeout",
  "json parse error: unterminated string",
]

export const llmCalls: LlmCall[] = Array.from({ length: 460 }, (_, idx) => {
  const id = idx + 1
  const purpose = rng.weighted(PURPOSES_W, [40, 26, 10, 8, 16])
  // Cada ~25 llamadas, una usa el modelo NO tabulado (cost 0 silencioso).
  const untab = idx % 47 === 0
  const model = untab ? "deepseek-v4-flash-preview" : rng.pick(PURPOSE_MODELS[purpose])
  const promptTokens = rng.int(400, purpose === "summarize" ? 6000 : 3000)
  const cacheHitTokens = rng.bool(0.6) ? Math.floor(promptTokens * rng.float(0.2, 0.8)) : 0
  const completionTokens = rng.int(80, purpose === "extract" ? 900 : 500)
  const statusRoll = rng.float(0, 1)
  const status = statusRoll < 0.05 ? "error" : statusRoll < 0.08 ? "filtered" : "ok"
  const latencyMs =
    rng.bool(0.06) ? rng.int(4000, 9000) : rng.int(350, 2600) // cola de p99 ocasional
  const cost = status === "ok" ? costOf(model, promptTokens, cacheHitTokens, completionTokens) : 0
  // individual referencia un inbox real; batch (summarize agregado) deja inbox_id null.
  const isIndividual = purpose === "extract" || (purpose === "summarize" && rng.bool(0.3))
  return {
    id,
    requestId: `req_${(id * 7919).toString(36)}`,
    inboxId: isIndividual ? rng.int(1, inbox.length) : null,
    purpose,
    model,
    promptTokens,
    completionTokens,
    cacheHitTokens,
    costUsd: cost,
    latencyMs,
    status,
    errorMessage: status === "error" ? rng.pick(ERR_MSGS) : null,
    createdAt: iso(rng.skewed(MIN, 14 * DAY)),
  }
})

// ---- ingestion_runs -----------------------------------------------------------

let runSeq = 0
function uuid(): string {
  runSeq += 1
  const h = (n: number) => (n * 2654435761).toString(16).padStart(8, "0").slice(0, 8)
  return `${h(runSeq)}-${h(runSeq + 1).slice(0, 4)}-4${h(runSeq + 2).slice(0, 3)}-a${h(runSeq + 3).slice(0, 3)}-${h(runSeq + 4)}${h(runSeq + 5).slice(0, 4)}`
}

const ingestionRuns: IngestionRun[] = []
for (const source of INGESTING) {
  // Instagram: última corrida hace >30h (source sin sync) → alerta.
  const stale = source.id === 5
  const runs = rng.int(10, 16)
  for (let k = 0; k < runs; k++) {
    const startedMsAgo = (stale ? 30 * HOUR : 0) + k * rng.int(4 * HOUR, 10 * HOUR) + rng.int(0, HOUR)
    const inserted = rng.int(0, 40)
    const duplicates = rng.int(0, 25)
    const errors = rng.bool(0.1) ? rng.int(1, 4) : 0
    const filtered = rng.int(0, source.type === "imap" ? 60 : 10)
    const posted = inserted + duplicates + errors + filtered
    ingestionRuns.push({
      id: uuid(),
      sourceId: source.id,
      trigger: source.config.mode === "streaming" ? "streaming" : "poll",
      status: "ok",
      startedAt: iso(startedMsAgo),
      endedAt: iso(startedMsAgo - rng.int(2000, 30000)),
      durationMs: rng.int(800, 24000),
      posted,
      inserted,
      duplicates,
      errors,
      filtered,
      errorClass: null,
      errorMessage: null,
    })
  }
}
// Correo universitario: última corrida FALLIDA (auth/SSO) hace ~5h.
ingestionRuns.push({
  id: uuid(),
  sourceId: 1,
  trigger: "poll",
  status: "failed",
  startedAt: iso(5 * HOUR),
  endedAt: iso(5 * HOUR - 4000),
  durationMs: 4000,
  posted: 0,
  inserted: 0,
  duplicates: 0,
  errors: 0,
  filtered: 0,
  errorClass: "IMAPLoginError",
  errorMessage: "AUTHENTICATE failed: token expirado (re-autorizar OAuth)",
})
// Gmail: una corrida con DESCUADRE del invariante (posted ≠ suma) — bug de contabilidad.
ingestionRuns.push({
  id: uuid(),
  sourceId: 2,
  trigger: "poll",
  status: "ok",
  startedAt: iso(7 * HOUR),
  endedAt: iso(7 * HOUR - 9000),
  durationMs: 9000,
  posted: 100,
  inserted: 60,
  duplicates: 24,
  errors: 2,
  filtered: 12, // 60+24+2+12 = 98 ≠ 100
  errorClass: null,
  errorMessage: null,
})
// Telegram canales: una corrida en curso (streaming catch-up).
ingestionRuns.push({
  id: uuid(),
  sourceId: 4,
  trigger: "streaming",
  status: "running",
  startedAt: iso(3 * MIN),
  endedAt: null,
  durationMs: null,
  posted: 12,
  inserted: 9,
  duplicates: 3,
  errors: 0,
  filtered: 0,
  errorClass: null,
  errorMessage: null,
})

export { ingestionRuns }

// ---- worker_runs --------------------------------------------------------------

function workerStats(job: WorkerJob): Record<string, number | Record<string, number>> {
  switch (job) {
    case "classify":
      return { scanned: rng.int(200, 900), classified: rng.int(200, 900), by_tier: { blacklist: rng.int(20, 120), batch: rng.int(150, 700), individual: rng.int(5, 60) } }
    case "summarize":
      return { messages: rng.int(50, 400), summarized: rng.int(20, 120), skipped: rng.int(0, 30), errors: rng.int(0, 6), by_tier: { batch: rng.int(10, 90), individual: rng.int(5, 40) } }
    case "extract":
      return { routed: rng.int(10, 80), extracted: rng.int(0, 30), by_module: { finance: rng.int(0, 12), calendar: rng.int(0, 18) } }
    case "calendar":
      return { pulled: rng.int(0, 600), created: rng.int(0, 40), modified: rng.int(0, 25), deleted: rng.int(0, 8), dedup_pairs: rng.int(0, 15), conflicts: rng.int(0, 4) }
    case "ocr":
      return { scanned: rng.int(0, 60), ok: rng.int(0, 50), errors: rng.int(0, 5), skipped: rng.int(0, 20), dedup_hits: rng.int(0, 18) }
  }
}

const workerRuns: WorkerRun[] = []
let wId = 0
const JOB_KEYS: WorkerJob[] = ["classify", "summarize", "extract", "calendar", "ocr"]
for (const job of JOB_KEYS) {
  const runs = rng.int(5, 9)
  for (let k = 0; k < runs; k++) {
    const startedMsAgo = k * rng.int(2 * HOUR, 8 * HOUR) + rng.int(0, HOUR)
    const dur = rng.int(3000, 90000)
    const status = rng.bool(0.12) ? "error" : "ok"
    workerRuns.push({
      id: ++wId,
      job,
      status,
      stats: workerStats(job),
      error: status === "error" ? "httpx.ConnectError: conexión rechazada" : null,
      startedAt: iso(startedMsAgo),
      finishedAt: iso(startedMsAgo - dur),
    })
  }
}
// Summarize: corrida COLGADA (running, iniciada hace 47 min > 30 min) → daemon huérfano.
workerRuns.push({
  id: ++wId,
  job: "summarize",
  status: "running",
  stats: { messages: 180, summarized: 40, skipped: 2, errors: 0 },
  error: null,
  startedAt: iso(47 * MIN),
  finishedAt: null,
})
// Extract: corrida con ERROR por 402 (saldo DeepSeek agotado) hace ~2h.
workerRuns.push({
  id: ++wId,
  job: "extract",
  status: "error",
  stats: { routed: 22, extracted: 4, by_module: { finance: 1, calendar: 3 } },
  error: "LLMQuotaError: 402 — saldo agotado; la corrida abortó (no se descartó nada)",
  startedAt: iso(2 * HOUR),
  finishedAt: iso(2 * HOUR - 12000),
})

export { workerRuns }

// ---- Cola de revisión: dead-letter + conflictos + dedup -----------------------

const DEADLETTER_ERRORS = [
  "openai.APIStatusError: 400 content_filter — la ventana siempre dispara el filtro",
  "json parse error: 'Expecting property name' (salida nunca parsea)",
  "ValueError: finish_reason='length' — la ventana siempre trunca",
]
const deadLetters: WorkItemFailure[] = Array.from({ length: 5 }, (_, k) => ({
  id: k + 1,
  stage: rng.pick(["summarize", "extract"]),
  inboxId: rng.int(1, inbox.length),
  attempts: 3,
  lastError: rng.pick(DEADLETTER_ERRORS),
  status: "review",
  createdAt: iso(rng.int(6 * HOUR, 3 * DAY)),
  updatedAt: iso(rng.int(MIN, 6 * HOUR)),
}))

const conflicts: CalendarConflict[] = [
  {
    id: 1,
    a: { id: 11, title: "Vuelo BOG → MEX", startsOn: "2026-06-12", endsOn: null, startTime: "08:30", endTime: "11:45", location: "Aeropuerto El Dorado", priorityRank: 100, protected: true },
    b: { id: 12, title: "Dentista — control", startsOn: "2026-06-12", endsOn: null, startTime: "09:00", endTime: "10:00", location: "Clínica Norte", priorityRank: 60, protected: false },
    reason: "Solape horario 09:00–10:00; ambos de alta importancia",
    status: "pending",
    createdAt: iso(8 * HOUR),
    instanceCount: 1,
    recurring: false,
    firstOn: "2026-06-12",
    lastOn: "2026-06-12",
  },
  {
    id: 2,
    a: { id: 21, title: "Defensa de tesis", startsOn: "2026-06-18", endsOn: null, startTime: "10:00", endTime: "12:00", location: "Aula Magna", priorityRank: 100, protected: true },
    b: { id: 22, title: "Entrevista laboral (remota)", startsOn: "2026-06-18", endsOn: null, startTime: "11:00", endTime: "11:45", location: "Google Meet", priorityRank: 80, protected: false },
    reason: "Solape horario 11:00–11:45; ambos de alta importancia",
    status: "pending",
    createdAt: iso(28 * HOUR),
    instanceCount: 1,
    recurring: false,
    firstOn: "2026-06-18",
    lastOn: "2026-06-18",
  },
]

const dedupCandidates: CalendarDedupCandidate[] = [
  {
    id: 1,
    a: { id: 31, title: "Cena de fin de año", startsOn: "2026-06-20", startTime: "21:00", location: "Casa de Ana", origin: "extraction", provider: null },
    b: { id: 32, title: "Cena fin de año 🎉", startsOn: "2026-06-20", startTime: null, location: "", origin: "provider", provider: "google" },
    reason: "Mismo título aproximado y misma fecha; hora difiere",
    score: 0.86,
    status: "candidate",
    createdAt: iso(12 * HOUR),
  },
  {
    id: 2,
    a: { id: 41, title: "Parcial de Sistemas", startsOn: "2026-06-09", startTime: "09:00", location: "Aula 204", origin: "extraction", provider: null },
    b: { id: 42, title: "Examen Sistemas Operativos", startsOn: "2026-06-09", startTime: "09:00", location: "Aula 204", origin: "extraction", provider: null },
    reason: "Misma fecha, hora y lugar; títulos similares",
    score: 0.92,
    status: "candidate",
    createdAt: iso(20 * HOUR),
  },
]

export const reviewItems: ReviewItem[] = [
  ...deadLetters.map((d): ReviewItem => ({ id: `dl-${d.id}`, kind: "dead-letter", at: d.updatedAt, deadLetter: d })),
  ...conflicts.map((c): ReviewItem => ({ id: `cf-${c.id}`, kind: "conflict", at: c.createdAt, conflict: c })),
  ...dedupCandidates.map((d): ReviewItem => ({ id: `dd-${d.id}`, kind: "dedup", at: d.createdAt, dedup: d })),
].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())

// ---- Alertas (centro persistente) ---------------------------------------------

export const seedAlerts: AlertEvent[] = [
  {
    id: "al-402",
    severity: "critica",
    kind: "saldo",
    title: "Saldo LLM agotado (402)",
    detail: "La corrida de extract abortó: DeepSeek devolvió 402. No se procesaron más ventanas.",
    at: iso(2 * HOUR),
    read: false,
    deepLink: "/pipeline",
  },
  {
    id: "al-stale",
    severity: "alta",
    kind: "worker-stale",
    title: "Worker summarize colgado",
    detail: "La corrida lleva 47 min en 'running' (umbral 30 min) — posible daemon caído.",
    at: iso(15 * MIN),
    read: false,
    deepLink: "/pipeline",
  },
  {
    id: "al-run",
    severity: "alta",
    kind: "run-failed",
    title: "Ingesta fallida: Correo universitario",
    detail: "AUTHENTICATE failed — token OAuth expirado, re-autorizar.",
    at: iso(5 * HOUR),
    read: false,
    deepLink: "/pipeline",
  },
  {
    id: "al-src",
    severity: "alta",
    kind: "source-stale",
    title: "Instagram sin sincronizar",
    detail: "Última corrida hace más de 30 h. ¿Se cayó el scraper?",
    at: iso(30 * HOUR),
    read: true,
    deepLink: "/pipeline",
  },
  {
    id: "al-review",
    severity: "info",
    kind: "review",
    title: `${reviewItems.length} ítems pendientes de revisión`,
    detail: "Dead-letter + conflictos de calendario + dedup esperan decisión humana.",
    at: iso(40 * MIN),
    read: false,
    deepLink: "/revision",
  },
]

// Conveniencias
export { SOURCES }
export const sourceById = (id: number): Source | undefined => SOURCES.find((s) => s.id === id)
