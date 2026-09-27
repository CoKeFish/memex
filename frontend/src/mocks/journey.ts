import { Rng } from "@/lib/rng"
import { renderPayload } from "@/lib/render-payload"
import type {
  JourneyStep,
  LlmExchange,
  LogEventRow,
  MediaAsset,
  MessageJourney,
  RelatedRecord,
  Tier,
} from "@/types/domain"
import { inbox } from "./index"
import { SOURCE_BY_ID } from "./catalog"

const MIN = 60_000

function reqHex(seed: number): string {
  const r = new Rng(seed)
  let s = ""
  for (let i = 0; i < 16; i++) s += "0123456789abcdef"[r.int(0, 15)]
  return s
}

function pickEvidence(body: string, tokens: string[]): string {
  for (const tk of tokens) {
    const i = body.toLowerCase().indexOf(tk.toLowerCase())
    if (i >= 0) {
      const start = Math.max(0, i - 12)
      return body.slice(start, Math.min(body.length, i + tk.length + 24)).trim()
    }
  }
  return body.slice(0, 48).trim()
}

function deriveTier(body: string, rng: Rng): { tier: Tier; rule: string } {
  const b = body.toLowerCase()
  if (b.includes("unsubscribe") || b.includes("newsletter") || b.includes("resumen semanal")) {
    return { tier: "blacklist", rule: "list_unsubscribe presente" }
  }
  if (b.includes("factura") || b.includes("cargo") || b.includes("reunión") || b.includes("entrega") || b.includes("parcial")) {
    return rng.bool(0.4) ? { tier: "individual", rule: "promovido (alta señal)" } : { tier: "batch", rule: "default (sin marcadores de bulk)" }
  }
  return { tier: "batch", rule: "default (sin marcadores de bulk)" }
}

const OCR_TEXT: Record<"factura" | "flyer" | "captura", string> = {
  factura: "FACTURA\nRailway Inc.\nN.º rw-8842\nTotal: US$2.54\nPeriodo: mayo 2026\nMétodo: tarjeta ••4291",
  flyer: "HACKATHON LATAM 2026\nRegistro hasta el 5 de junio\nAula Magna · 9:00 hs\nPremios · mentorías · comida",
  captura: "Captura de pantalla\n(texto reconocido por el modelo de visión)\n— sin datos estructurados —",
}

function buildMedia(
  row: { id: number; sourceId: number; payload: Record<string, unknown> },
  rng: Rng,
  body: string,
  sourceType: string,
): MediaAsset[] {
  const p = row.payload
  const out: MediaAsset[] = []
  let aid = row.id * 10
  const topic: "factura" | "flyer" | "captura" = /factura|cargo|us\$|\$|pago|railway/i.test(body)
    ? "factura"
    : /hackathon|reuni|entrega|parcial|evento|flyer|invitaci|vuelo|registro/i.test(body)
      ? "flyer"
      : "captura"
  const sha = (): string => {
    let s = ""
    for (let i = 0; i < 12; i++) s += "0123456789abcdef"[rng.int(0, 15)]
    return s
  }
  const img = (filename: string, contentType = "image/png") => {
    const dedupHit = rng.bool(0.18)
    out.push({
      id: aid++,
      sha256: sha(),
      objectKey: `u1/${sha()}.${contentType.split("/")[1]}`,
      bucket: "memex-media",
      contentType,
      sizeBytes: rng.int(40_000, 900_000),
      filename,
      extension: contentType.split("/")[1] ?? null,
      ocrStatus: "ok",
      ocrModel: "vision-ocr-1",
      ocrText: OCR_TEXT[topic],
      ocrError: null,
      ocrAttempts: dedupHit ? 0 : 1,
      truncated: rng.bool(0.12),
      dedupHit,
    })
  }
  const pdf = (filename: string) => {
    out.push({
      id: aid++,
      sha256: sha(),
      objectKey: `u1/${sha()}.pdf`,
      bucket: "memex-media",
      contentType: "application/pdf",
      sizeBytes: rng.int(50_000, 1_200_000),
      filename,
      extension: "pdf",
      ocrStatus: "skipped",
      ocrModel: null,
      ocrText: "",
      ocrError: null,
      ocrAttempts: 0,
      truncated: false,
      dedupHit: false,
    })
  }
  const mk = typeof p.media_kind === "string" ? p.media_kind : ""
  if (sourceType === "telegram") {
    if (mk === "photo") img(`foto_${p.message_id ?? row.id}.jpg`, "image/jpeg")
    else if (mk === "sticker") img("sticker.webp", "image/webp")
    else if (mk === "document") pdf("archivo.pdf")
  } else if (sourceType === "social") {
    const platform = typeof p.platform === "string" ? p.platform : "post"
    if (mk === "image" || mk === "carousel" || mk === "reel") img(`${platform}_${p.post_id ?? row.id}.jpg`, "image/jpeg")
  } else {
    const atts = Array.isArray(p.attachments) ? (p.attachments as unknown[]) : []
    if (atts.length) {
      img(topic === "factura" ? "recibo.png" : "adjunto.png")
      if (rng.bool(0.4)) pdf("documento.pdf")
    }
  }
  return out
}

export function getMessageJourney(inboxId: number): MessageJourney | null {
  const row = inbox.find((r) => r.id === inboxId)
  if (!row) return null

  const rng = new Rng((inboxId * 2654435761) >>> 0)
  const rid = reqHex(inboxId)
  const rendered = renderPayload(row.payload, row.ocrText ?? "")
  const body = rendered.body || rendered.line
  const src = SOURCE_BY_ID[row.sourceId]
  const baseMs = new Date(row.receivedAt).getTime()
  const at = (offsetMin: number) => new Date(baseMs + offsetMin * MIN).toISOString()

  const media = buildMedia({ id: row.id, sourceId: row.sourceId, payload: row.payload as unknown as Record<string, unknown> }, rng, body, src?.type ?? "imap")
  const okOcr = media.filter((m) => m.ocrStatus === "ok")
  const ocrSuffix = okOcr.length ? " + texto OCR de adjuntos" : ""

  const steps: JourneyStep[] = []
  const logs: LogEventRow[] = []
  const srcId = row.sourceId
  let logSeq = 0
  function addLog(event: string, module: string, level: LogEventRow["level"], offsetMin: number, fields: Record<string, unknown>) {
    logs.push({
      id: inboxId * 1000 + logSeq++,
      ts: at(offsetMin),
      level,
      event,
      logger: `memex.${module}`,
      requestId: rid,
      userId: 1,
      runId: null,
      sourceId: srcId,
      inboxId,
      exception: null,
      fields,
    })
  }

  // 1. Ingesta
  steps.push({
    kind: "ingesta",
    title: "Ingesta",
    at: row.receivedAt,
    summary: `Recibido desde ${src?.name ?? row.sourceId} y persistido en inbox.`,
    details: [
      { label: "external_id", value: row.externalId },
      { label: "occurred_at", value: row.occurredAt },
      { label: "dedupe", value: `clave única (source ${row.sourceId})` },
    ],
    tone: "neutral",
  })
  addLog("persist.inserted", "persist", "info", 0, { inbox_id: inboxId, source: src?.name })

  // 2. OCR / multimodal (si hay media) — su texto alimenta render → resumen/extracción
  if (media.length) {
    const dedup = media.filter((m) => m.dedupHit).length
    const provenance =
      src?.type === "telegram"
        ? "del chat de Telegram (Telethon download_media)"
        : src?.type === "social"
          ? `imagen del post de ${src?.name ?? "social"} (del scraper)`
          : "adjunto del correo"
    steps.push({
      kind: "ocr",
      title: "OCR · modelo multimodal",
      at: at(1),
      summary: `Media ${provenance}. El modelo multimodal procesó ${okOcr.length} imagen(es); su texto se inyecta al render para resumen y extracción.${dedup ? ` ${dedup} evitada(s) por dedup sha256.` : ""}`,
      details: [
        { label: "tabla", value: "media_assets" },
        { label: "assets", value: String(media.length) },
        { label: "ocr ok", value: String(okOcr.length) },
      ],
      media,
      tone: okOcr.length ? "ok" : "neutral",
    })
    addLog("ocr.run.start", "ocr", "info", 1, { inbox_id: inboxId, assets: media.length })
    for (const m of okOcr) addLog("llm.call", "llm", "info", 1, { purpose: "ocr", model: m.ocrModel, inbox_id: inboxId })
  }

  // 3. Clasificación (reglas, sin LLM)
  const { tier, rule } = deriveTier(body, rng)
  steps.push({
    kind: "clasificacion",
    title: "Clasificación",
    at: at(2),
    summary: `Clasificado como tier "${tier}" por regla determinista (sin LLM).`,
    details: [
      { label: "tier", value: tier },
      { label: "regla", value: rule },
    ],
    tone: tier === "blacklist" ? "neutral" : tier === "individual" ? "review" : "running",
  })
  addLog("classifier.run.end", "classifier", "info", 2, { tier })

  if (tier !== "blacklist") {
    // 4. Ruteo
    const wantsFinance = /factura|cargo|us\$|\$|pago|railway/i.test(body) || media.some((m) => m.ocrText.includes("FACTURA"))
    const wantsCalendar = /reuni[oó]n|entrega|jueves|parcial|hackathon|cita|vuelo/i.test(body) || media.some((m) => m.ocrText.includes("HACKATHON"))
    const chosen = [wantsFinance && "finance", wantsCalendar && "calendar"].filter(Boolean) as string[]
    const dropped = ["finance", "calendar"].filter((m) => !chosen.includes(m))
    steps.push({
      kind: "ruteo",
      title: "Ruteo de módulos",
      at: at(4),
      summary: chosen.length ? `El router eligió: ${chosen.join(", ")}.` : "El router no eligió ningún módulo (short-circuit).",
      details: [
        { label: "elegidos", value: chosen.join(", ") || "—" },
        { label: "descartados", value: dropped.join(", ") || "—" },
      ],
      tone: "neutral",
    })
    addLog("route.decision", "route", "info", 4, { chosen: chosen.join(",") || "none", inbox_id: inboxId })
    if (dropped.length) addLog("route.dropped", "route", "info", 4, { dropped: dropped.join(","), inbox_id: inboxId })

    // 5. Módulos (el input incluye el texto OCR si lo hubo)
    if (chosen.includes("finance")) {
      const fromOcr = okOcr.some((m) => m.ocrText.includes("FACTURA"))
      const evidence = pickEvidence(fromOcr ? okOcr[0].ocrText : body, ["US$", "$", "Total", "factura", "cargo"])
      const llm: LlmExchange = {
        purpose: "extract",
        model: "deepseek-v4-flash",
        promptTokens: rng.int(800, 2400),
        completionTokens: rng.int(120, 320),
        costUsd: Number(rng.float(0.001, 0.006).toFixed(6)),
        latencyMs: rng.int(600, 2200),
        status: "ok",
        inputSummary: `render_payload(inbox #${inboxId})${ocrSuffix} + prompt de finance (anti-publicidad)`,
        output: JSON.stringify(
          { amount: 2.54, currency: "USD", merchant: "Railway", occurred_on: "2026-05-28", source_inbox_ids: [inboxId], evidence },
          null,
          2,
        ),
      }
      steps.push({
        kind: "modulo",
        title: "Módulo finance",
        at: at(6),
        summary: fromOcr ? "Extrajo 1 gasto desde el texto OCR de la imagen." : "Extrajo 1 gasto con atribución por-mensaje.",
        details: [
          { label: "tabla", value: "mod_finance_expenses" },
          { label: "items", value: "1" },
          ...(fromOcr ? [{ label: "fuente", value: "texto OCR" }] : []),
        ],
        evidence: { quote: evidence, sourceText: fromOcr ? okOcr[0].ocrText : body },
        llm,
        tone: "ok",
      })
      addLog("llm.call", "llm", "info", 6, { purpose: "extract", model: llm.model, inbox_id: inboxId, cost_usd: llm.costUsd })
    }
    if (chosen.includes("calendar")) {
      const fromOcr = okOcr.some((m) => m.ocrText.includes("HACKATHON"))
      const evidence = pickEvidence(fromOcr ? okOcr[0].ocrText : body, ["jueves", "reunión", "entrega", "parcial", "hackathon", "vuelo", "Aula"])
      const llm: LlmExchange = {
        purpose: "extract",
        model: "deepseek-v4-flash",
        promptTokens: rng.int(800, 2400),
        completionTokens: rng.int(140, 360),
        costUsd: Number(rng.float(0.001, 0.006).toFixed(6)),
        latencyMs: rng.int(700, 2400),
        status: "ok",
        inputSummary: `render_payload(inbox #${inboxId})${ocrSuffix} + prompt de calendar (fechas)`,
        output: JSON.stringify(
          { title: "Evento extraído", starts_on: "2026-06-05", start_time: "10:00", location: "Aula 204", source_inbox_ids: [inboxId], evidence },
          null,
          2,
        ),
      }
      steps.push({
        kind: "modulo",
        title: "Módulo calendar",
        at: at(7),
        summary: fromOcr ? "Extrajo 1 evento desde el flyer (texto OCR)." : "Extrajo 1 evento (fecha/hora naive); pasa al dominio consolidado.",
        details: [
          { label: "tabla", value: "mod_calendar_events" },
          { label: "items", value: "1" },
          ...(fromOcr ? [{ label: "fuente", value: "texto OCR" }] : []),
        ],
        evidence: { quote: evidence, sourceText: fromOcr ? okOcr[0].ocrText : body },
        llm,
        tone: "ok",
      })
      addLog("llm.call", "llm", "info", 7, { purpose: "extract", model: llm.model, inbox_id: inboxId, cost_usd: llm.costUsd })
    }

    // 6. Resumen
    if (!row.processError) {
      const llm: LlmExchange = {
        purpose: "summarize",
        model: tier === "individual" ? "deepseek-v4-pro" : "deepseek-v4-flash",
        promptTokens: rng.int(1000, 5000),
        completionTokens: rng.int(120, 400),
        costUsd: Number(rng.float(0.0008, 0.01).toFixed(6)),
        latencyMs: rng.int(700, 2800),
        status: "ok",
        inputSummary: `${tier === "batch" ? "ventana conversacional (N mensajes)" : "1 mensaje (individual)"}${ocrSuffix}`,
        output: `${rendered.sender || "Remitente"} comunica: ${body.slice(0, 120)}${body.length > 120 ? "…" : ""}`,
      }
      steps.push({
        kind: "resumen",
        title: `Resumen (${tier})`,
        at: at(8),
        summary: tier === "batch" ? "Cubierto por un resumen batch (ventana N:M)." : "Resumen individual 1:1.",
        details: [{ label: "tabla", value: "summaries + summary_inbox_links" }],
        llm,
        tone: "ok",
      })
      addLog("llm.call", "llm", "info", 8, { purpose: "summarize", model: llm.model, inbox_id: inboxId, cost_usd: llm.costUsd })
    }
  }

  // 7. Dead-letter (si falló el procesamiento)
  if (row.processError) {
    steps.push({
      kind: "deadletter",
      title: "Dead-letter",
      at: at(9),
      summary: "El procesamiento falló de forma recuperable; tras 3 intentos pasa a revisión.",
      details: [
        { label: "attempts", value: `${row.attempts} / 3` },
        { label: "last_error", value: row.processError },
      ],
      tone: "error",
    })
    addLog("extract.item.invalid", "extract", "warning", 9, { inbox_id: inboxId, error: row.processError })
  }

  // Datos relacionados (registros concretos + cardinalidad)
  const related: RelatedRecord[] = [
    { table: "sources", relation: "inbox.source_id → sources.id", cardinality: "N:1", exposedByApi: true, keys: [{ label: "source_id", value: String(row.sourceId) }, { label: "name", value: src?.name ?? "" }] },
    { table: "classifications", relation: "UNIQUE(inbox_id)", cardinality: "1:1", exposedByApi: false, keys: [{ label: "tier", value: tier }] },
    { table: "summary_inbox_links", relation: "N:M vía summaries", cardinality: "N:M", exposedByApi: false, keys: [{ label: "links", value: tier === "blacklist" ? "0" : "1" }] },
    { table: "module_extractions", relation: "UNIQUE(module_slug, inbox_id)", cardinality: "1:N", exposedByApi: false, keys: [{ label: "módulos", value: tier === "blacklist" ? "—" : "finance/calendar" }] },
    { table: "media_assets", relation: "inbox_id FK (ON DELETE CASCADE)", cardinality: "1:N", exposedByApi: false, keys: [{ label: "assets", value: String(media.length) }, { label: "ocr ok", value: String(okOcr.length) }] },
    { table: "llm_calls", relation: "inbox_id (SET NULL)", cardinality: "1:N", exposedByApi: false, keys: [{ label: "llamadas", value: String(steps.filter((s) => s.llm).length + okOcr.length) }] },
    { table: "inbox_dedupe_keys", relation: "PK(user_id, key)", cardinality: "1:N", exposedByApi: false, keys: [{ label: "key", value: row.externalId }] },
  ]

  logs.sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime())
  return { row, steps, logs, related, media }
}

// Algunos inbox_id representativos para enlazar desde demos.
export const FEATURED_JOURNEYS = [2, 3, 4].filter((id) => id <= inbox.length)
