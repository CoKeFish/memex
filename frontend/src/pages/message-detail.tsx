import { useMemo, useState } from "react"
import { ArrowLeft, Braces, CalendarDays, DollarSign, Eye, Loader2, Network, Paperclip, RotateCw, ScrollText, Sparkles, Trophy, Users, Zap } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { StatusBadge } from "@/components/common/led"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { RelativeTime } from "@/components/common/time"
import { JourneyTimeline } from "@/components/features/message/journey-timeline"
import { LlmTrace } from "@/components/features/message/llm-trace"
import { TraceTree } from "@/components/features/message/trace-tree"
import { MediaOcr, UnstoredAttachments, type DeclaredAttachment } from "@/components/features/message/media-ocr"
import { fmtCost, groupCallsIntoRuns } from "@/components/features/message/llm-trace-runs"
import { RelatedData } from "@/components/features/message/related-data"
import { ReprocessButton } from "@/components/features/message/reprocess-button"
import { reprocessStepsFor, type ReprocessStep } from "@/components/features/message/reprocess-steps"
import { FeedbackButton } from "@/components/features/message/feedback-button"
import { RelevanceButton } from "@/components/features/message/relevance-button"
import { MessageFilterMenu } from "@/components/features/message/message-filter-menu"
import { LogRow } from "@/components/features/logs/log-row"
import {
  extractInboxItem,
  fetchInboxItem,
  fetchSources,
  getMessageJourney,
  processInboxItem,
  SOURCE_BY_ID,
  summarizeInboxItem,
  type ProcessScope,
} from "@/data"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { renderPayload } from "@/lib/render-payload"
import { sourceMeta } from "@/lib/inbox-format"
import { formatDateOnly } from "@/lib/format"
import {
  ATTACHMENT_ICON,
  ATTACHMENT_LABEL,
  attachmentKind,
  uniqueKinds,
  type AttachmentKind,
} from "@/lib/attachment-kind"
import type { Tone } from "@/lib/status"
import type {
  ExtractionDebug,
  FinanceDebugRow,
  IdentidadesDebugRow,
  InboxLlmCall,
  InboxRow,
  InternalLlmCall,
  MessageJourney,
  Source,
} from "@/types/domain"

const TIER_META: Record<string, { label: string; tone: Tone }> = {
  blacklist: { label: "Blacklist", tone: "filtered" },
  batch: { label: "Lote", tone: "running" },
  individual: { label: "Individual", tone: "review" },
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

function str(v: unknown): string {
  return v == null ? "" : String(v)
}

function statusOf(row: InboxRow): { tone: Tone; label: string } {
  if (row.processError) return { tone: "error", label: "Error" }
  if (row.processedAt) return { tone: "ok", label: "Procesado" }
  return { tone: "pending", label: "Pendiente" }
}

function BackLink() {
  return (
    <Button variant="ghost" size="sm" className="h-8" asChild>
      <Link to="/datos">
        <ArrowLeft className="size-4" /> Datos
      </Link>
    </Button>
  )
}

function PageShell({ children }: { children: React.ReactNode }) {
  return <div className="space-y-4">{children}</div>
}

export function MessageDetailPage() {
  const { id } = useParams()
  const numId = Number(id)
  const { data: row, loading, error, reload } = useAsync<InboxRow | null>(
    () => fetchInboxItem(numId),
    [numId],
  )

  if (loading) {
    return (
      <PageShell>
        <BackLink />
        <Panel>
          <div className="flex items-center gap-2 px-4 py-14 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando mensaje…
          </div>
        </Panel>
      </PageShell>
    )
  }
  if (error) {
    return (
      <PageShell>
        <BackLink />
        <Panel>
          <ErrorState detail={error} />
        </Panel>
      </PageShell>
    )
  }
  if (row) return <RealDetail row={row} onProcessed={reload} />

  // Fallback a la demo mock (p. ej. enlaces desde la cola de revisión a ids inexistentes en la DB).
  const journey = getMessageJourney(numId)
  if (journey) return <MockDetail journey={journey} />

  return (
    <PageShell>
      <BackLink />
      <Panel>
        <EmptyState title="Mensaje no encontrado" hint={`No existe el inbox #${id}.`} />
      </Panel>
    </PageShell>
  )
}

function RealDetail({ row, onProcessed }: { row: InboxRow; onProcessed: () => void }) {
  const [raw, setRaw] = useState(false)
  const [showBody, setShowBody] = useState(false)
  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const source = (sources ?? []).find((s) => s.id === row.sourceId)
  const meta = sourceMeta(source)
  const SrcIcon = meta.icon
  const rendered = renderPayload(row.payload, row.ocrText ?? "")
  const cls = row.classification
  const subject = emailSubject(row.payload)
  // El asunto se muestra aparte (debajo del título); recortamos el cuerpo desde el body_text real
  // para no duplicar el "Asunto:" (el asunto puede tener saltos de línea, así que no sirve un regex).
  const rawBody = bodyTextOf(row.payload)
  const bodyText =
    rawBody && rendered.body.includes(rawBody)
      ? rendered.body.slice(rendered.body.indexOf(rawBody))
      : rendered.body

  const declared = declaredAttachments(row.payload)
  const media = row.media ?? []
  const hasAttachments = declared.length > 0 || media.length > 0
  const attSource = declared.length
    ? declared.map((a) => ({ ct: a.contentType, name: a.filename }))
    : media.map((m) => ({ ct: m.contentType, name: m.extension ?? m.filename }))
  const attachKinds = uniqueKinds(attSource.map((a) => attachmentKind(a.ct, a.name)))

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <BackLink />
        <span className="eyebrow">detalle del mensaje</span>
      </div>

      <Panel className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="num text-sm text-muted-foreground">inbox #{row.id}</span>
              <span className={cn("inline-flex items-center gap-1.5 text-sm font-semibold", meta.tone)}>
                <SrcIcon className="size-3.5" /> {meta.label}
              </span>
              {cls && <StatusBadge tone={TIER_META[cls.tier]?.tone ?? "neutral"} label={TIER_META[cls.tier]?.label ?? cls.tier} />}
            </div>
            <div className="num mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
              <span>{row.externalId}</span>
              <span>occurred <RelativeTime date={row.occurredAt} /></span>
              <span>received <RelativeTime date={row.receivedAt} /></span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MessageFilterMenu row={row} sourceType={source?.type ?? null} onDone={onProcessed} />
            <RelevanceButton inboxId={row.id} current={row.relevance} onDone={onProcessed} />
            <FeedbackButton inboxId={row.id} current={row.feedback} onDone={onProcessed} />
            <ReprocessButton inboxId={row.id} steps={reprocessStepsForRow(row)} onDone={onProcessed} />
            <Button variant="outline" size="sm" asChild>
              <Link to={`/grafo?inbox_id=${row.id}`} title="Ver en el grafo lo que produjo este mensaje">
                <Network className="size-3.5" /> Ver en grafo
              </Link>
            </Button>
            <Label htmlFor="raw-detail" className="eyebrow cursor-pointer">JSON crudo</Label>
            <Switch id="raw-detail" checked={raw} onCheckedChange={setRaw} />
          </div>
        </div>
        {raw ? (
          <pre className="mt-3 max-h-80 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-[11px] text-muted-foreground">
            {JSON.stringify(row.payload, null, 2)}
          </pre>
        ) : (
          <div className={cn("mt-3 grid gap-3", hasAttachments && "lg:grid-cols-[4fr_3fr]")}>
            {/* Cuerpo: nombre + asunto + el toggle "cuerpo del correo" SIEMPRE arriba (así se colapsa
                sin scrollear todo el texto). Colapsado = preview que rellena la columna con
                degradado y también expande al clic; expandido = texto completo. */}
            <div className="flex min-w-0 flex-col rounded-md border border-border bg-muted/20 p-3">
              {rendered.sender && <div className="text-sm font-medium">{rendered.sender}</div>}
              {subject && (
                <div className="mt-0.5 break-words text-sm text-muted-foreground">{subject}</div>
              )}
              <button
                type="button"
                onClick={() => setShowBody((v) => !v)}
                className="eyebrow mt-2 flex items-center gap-1 self-start hover:text-foreground"
              >
                cuerpo del correo {showBody ? "▴" : "▾"}
              </button>
              {showBody ? (
                <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                  {bodyText || "(sin texto)"}
                </p>
              ) : (
                // Preview ABSOLUTO: no aporta altura, así la fila la marca la columna de adjuntos y
                // el cuerpo se recorta para rellenar el espacio (flex-1) con un degradado abajo.
                <button
                  type="button"
                  onClick={() => setShowBody(true)}
                  className="relative mt-2 min-h-[4rem] flex-1 text-left"
                >
                  <div className="absolute inset-0 overflow-hidden">
                    <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">
                      {bodyText || "(sin texto)"}
                    </p>
                  </div>
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-card via-card/80 to-transparent" />
                </button>
              )}
            </div>

            {/* Adjuntos a la derecha; el "lo que vio el modelo multimodal" colapsa por adjunto. */}
            {hasAttachments && (
              <div className="min-w-0 rounded-md border border-border bg-muted/20 p-3">
                <div className="mb-2 flex items-center gap-1.5 text-sm font-medium">
                  <Paperclip className="size-3.5 text-muted-foreground" />
                  Adjuntos · {declared.length || media.length}
                  <span className="ml-auto">
                    <AttachmentIconsRow kinds={attachKinds} />
                  </span>
                </div>
                <AttachmentsContent row={row} />
              </div>
            )}
          </div>
        )}
      </Panel>

      <PipelinePanel row={row} onProcessed={onProcessed} />
    </div>
  )
}

/** Adjuntos declarados en el payload (siempre presentes), aunque no se hayan almacenado/OCR-eado. */
function declaredAttachments(payload: unknown): DeclaredAttachment[] {
  const p = payload as { attachments?: unknown }
  if (!Array.isArray(p?.attachments)) return []
  return (p.attachments as Record<string, unknown>[]).map((a) => ({
    filename: typeof a?.filename === "string" ? a.filename : null,
    contentType: String(a?.content_type ?? ""),
    size: Number(a?.size ?? 0),
  }))
}

/** Etapas reprocesables según el ESTADO REAL del mensaje (no el mock journey). */
function reprocessStepsForRow(row: InboxRow): ReprocessStep[] {
  const out: ReprocessStep[] = []
  const media = row.media ?? []
  const declared = declaredAttachments(row.payload)
  const mediaNames = new Set(media.map((m) => m.filename))
  if (declared.some((a) => !mediaNames.has(a.filename)))
    out.push({ stage: "media", label: "Traer adjuntos (IMAP)", hint: "re-baja los declarados sin almacenar" })
  if (media.length > 0)
    out.push({ stage: "ocr", label: "Re-OCR de adjuntos", hint: "vuelve a transcribir los adjuntos" })
  out.push({ stage: "classify", label: "Re-clasificar", hint: "determinista · sin LLM" })
  if (row.classification) {
    out.push({ stage: "summarize", label: "Re-resumir", hint: "LLM" })
    out.push({ stage: "extract", label: "Re-extraer (módulos)", hint: "LLM · finanzas/calendario/hackatones" })
  }
  return out
}

/** Asunto del correo (vacío para chat/social). Se muestra debajo del título del cuerpo. */
function emailSubject(payload: unknown): string {
  const p = payload as { subject?: unknown }
  return typeof p?.subject === "string" ? p.subject.trim() : ""
}

/** Cuerpo crudo del payload (body_text / text / media_caption) — para recortar el asunto del render. */
function bodyTextOf(payload: unknown): string {
  const p = payload as { body_text?: unknown; text?: unknown; media_caption?: unknown }
  return String(p?.body_text || p?.text || p?.media_caption || "")
}

/** Cluster compacto de íconos por tipo de adjunto (hasta 4), con tooltip. */
function AttachmentIconsRow({ kinds }: { kinds: AttachmentKind[] }) {
  if (kinds.length === 0) return null
  return (
    <span
      className="flex items-center gap-0.5 text-muted-foreground"
      title={kinds.map((k) => ATTACHMENT_LABEL[k]).join(", ")}
    >
      {kinds.slice(0, 4).map((k, i) => {
        const Icon = ATTACHMENT_ICON[k]
        return <Icon key={i} className="size-3.5" />
      })}
    </span>
  )
}

/** Contenido del colapsable de adjuntos: media_assets (preview/OCR) + declarados sin almacenar. */
function AttachmentsContent({ row }: { row: InboxRow }) {
  const media = row.media ?? []
  const mediaNames = new Set(media.map((m) => m.filename))
  const unstored = declaredAttachments(row.payload).filter((a) => !mediaNames.has(a.filename))
  return (
    <div className="space-y-2.5">
      {media.length > 0 && <MediaOcr media={media} calls={row.llm?.items ?? []} />}
      {unstored.length > 0 && <UnstoredAttachments items={unstored} />}
    </div>
  )
}

type Phase = "classify" | "summarize" | "extract"

function PipelinePanel({ row, onProcessed }: { row: InboxRow; onProcessed: () => void }) {
  const [scope, setScope] = useState<ProcessScope>("individual")
  const [busy, setBusy] = useState<Phase | null>(null)
  const [showInput, setShowInput] = useState(false)

  const cls = row.classification
  const summary = row.summary
  const ext = row.extraction
  const llm = row.llm
  const trace = row.trace ?? null
  const extDone = !!ext?.done
  const extItems =
    (ext?.finance.length ?? 0) +
    (ext?.calendar.length ?? 0) +
    (ext?.hackathones.length ?? 0) +
    (ext?.identidades.length ?? 0)
  const input = renderPayload(row.payload, row.ocrText ?? "")
  // "Su lote" solo tiene sentido si el mensaje pertenece a un lote conversacional (tier batch);
  // para individual/blacklist la ventana es de 1 → el toggle sería un no-op (se deshabilita).
  const tier = cls?.tier
  const hasLote = tier === "batch"
  // Qué módulos EXTRAJERON de verdad (y en cuántas llamadas) se lee de la TRAZA, no de `ext.modules`
  // (que son los CONSIDERADOS, incluidos los ruteados-fuera).
  const extractionCalls = (llm?.items ?? []).filter((c) => c.purpose.startsWith("extract"))
  const extractedSlugs = extractedSlugsFromCalls(extractionCalls)
  const callShape = describeExtractionCalls(extractionCalls)

  // Corridas LLM (agrupadas por request_id / tiempo): alimentan la traza y los sellos de tiempo
  // "última corrida" de cada fase.
  const runs = useMemo(() => groupCallsIntoRuns(llm?.items ?? []), [llm])
  const summaryRunAt = runs.find((r) => r.producedSummary)?.startedAt ?? summary?.createdAt ?? null
  const extractRunAt = runs.find((r) => r.producedExtraction)?.startedAt ?? null

  async function run(phase: Phase, fn: () => Promise<void>) {
    setBusy(phase)
    try {
      await fn()
      onProcessed()
    } catch (e) {
      toast.error("No se pudo procesar", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  const classify = () =>
    run("classify", async () => {
      const r = await processInboxItem(row.id)
      const label = TIER_META[r.tier]?.label ?? r.tier
      toast.success(r.already ? `Ya clasificado: ${label}` : `Clasificado: ${label}`)
    })
  const summarize = (force: boolean) =>
    run("summarize", async () => {
      const r = await summarizeInboxItem(row.id, { scope, force })
      toast.success(r.status === "already" ? "Ya estaba resumido" : "Resumen listo", {
        description: [
          r.messages ? `${r.messages} msj` : null,
          r.calls ? `${r.calls} llamada(s) · ${fmtCost(r.cost_usd ?? 0)}` : null,
        ]
          .filter(Boolean)
          .join(" · "),
      })
    })
  const extract = (force: boolean) =>
    run("extract", async () => {
      const r = await extractInboxItem(row.id, { scope, force })
      const n = (r.finance?.length ?? 0) + (r.calendar?.length ?? 0) + (r.hackathones?.length ?? 0)
      toast.success(n > 0 ? `Extracción: ${n} dato(s)` : "Extracción: sin datos relevantes", {
        description: [
          (r.discarded ?? 0) > 0 ? `${r.discarded} descartado(s)` : null,
          r.calls ? `${r.calls} llamada(s) · ${fmtCost(r.cost_usd ?? 0)}` : null,
        ]
          .filter(Boolean)
          .join(" · "),
      })
    })

  return (
    <Panel>
      <PanelHeader
        eyebrow="pipeline"
        title="Procesamiento por fases"
        right={
          <div
            className="flex items-center gap-1 rounded-md border border-border p-0.5"
            title={
              hasLote
                ? "Alcance del resumen/extracción: solo este mensaje o todo su lote conversacional."
                : `Este mensaje se clasificó ${tier ? `«${tier}»` : "individual"}: no tiene lote (la ventana es de 1).`
            }
          >
            {(["individual", "window"] as ProcessScope[]).map((s) => {
              const disabled = s === "window" && !hasLote
              return (
                <button
                  key={s}
                  type="button"
                  disabled={disabled}
                  onClick={() => !disabled && setScope(s)}
                  className={cn(
                    "rounded px-2 py-0.5 text-[11px] transition-colors",
                    scope === s && !disabled
                      ? "bg-accent text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                    disabled && "cursor-not-allowed opacity-40 hover:text-muted-foreground",
                  )}
                >
                  {s === "individual" ? "Solo este" : "Su lote"}
                </button>
              )
            })}
          </div>
        }
      />
      <PanelBody className="py-0">
        {/* 2 columnas en xl: etapas a la izquierda, traza LLM + "qué hizo cada módulo" a la derecha
            (aprovecha el ancho; en pantallas chicas colapsa a una sola columna). */}
        <div className="grid gap-5 py-1 xl:grid-cols-2">
          <div className="min-w-0 divide-y divide-border">
        {/* Input — lo que ve el LLM (render_payload), colapsable para auditar */}
        <div className="py-2.5">
          <button
            type="button"
            onClick={() => setShowInput((v) => !v)}
            className="eyebrow flex items-center gap-1.5 hover:text-foreground"
          >
            <Eye className="size-3" /> Input al LLM (render_payload) {showInput ? "▾" : "▸"}
          </button>
          {showInput && (
            <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-2.5 font-mono text-[11px] text-muted-foreground">
              {input.line || input.body || "(vacío)"}
            </pre>
          )}
        </div>

        {/* Fase 1 — Clasificación (determinista, sin LLM) */}
        <Stage icon={Zap} title="Clasificación" hint="determinista · sin LLM" done={!!cls}>
          {cls ? (
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone={TIER_META[cls.tier]?.tone ?? "neutral"} label={TIER_META[cls.tier]?.label ?? cls.tier} />
              {typeof cls.metadata?.rule === "string" && (
                <span className="num text-xs text-muted-foreground">regla: {cls.metadata.rule}</span>
              )}
            </div>
          ) : (
            <PhaseButton label="Clasificar" icon={Zap} busy={busy === "classify"} disabled={busy !== null} onClick={classify} />
          )}
        </Stage>

        {/* Fase 2 — Resumen (LLM) */}
        <Stage icon={ScrollText} title="Resumen" hint="LLM" done={!!summary} blocked={!cls} at={summary ? summaryRunAt : null}>
          {!cls ? (
            <span className="text-xs text-muted-foreground">Clasificá primero.</span>
          ) : summary ? (
            <div className="space-y-2">
              <p className="whitespace-pre-wrap rounded-md border border-border bg-muted/20 p-2.5 text-sm text-muted-foreground">
                {summary.content}
              </p>
              <PhaseButton label="Rehacer" icon={RotateCw} variant="outline" busy={busy === "summarize"} disabled={busy !== null} onClick={() => summarize(true)} />
            </div>
          ) : (
            <PhaseButton label="Resumir" icon={ScrollText} busy={busy === "summarize"} disabled={busy !== null} onClick={() => summarize(false)} />
          )}
        </Stage>

        {/* Fase 3 — Extracción (LLM, módulos) */}
        <Stage icon={Sparkles} title="Extracción" hint="LLM · una llamada agrupada" done={extDone} blocked={!cls} at={extDone ? extractRunAt : null}>
          {!cls ? (
            <span className="text-xs text-muted-foreground">Clasificá primero.</span>
          ) : extDone ? (
            <div className="space-y-2">
              {/* Considerados (cursor, incluye ruteados-fuera) vs los que REALMENTE extrajeron (traza),
                  y en cuántas llamadas (1 agrupada por defecto). No mentir con "módulos corridos". */}
              <div className="space-y-0.5">
                {(ext?.modules.length ?? 0) > 0 && (
                  <div className="eyebrow">considerados: {ext!.modules.join(", ")}</div>
                )}
                {extractedSlugs.length > 0 && (
                  <div className="eyebrow">
                    extraídos: <span className="text-foreground">{extractedSlugs.join(", ")}</span>
                  </div>
                )}
                {callShape && <div className="num text-[10px] text-muted-foreground">{callShape}</div>}
              </div>
              {extItems === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Procesado · <span className="text-foreground">sin datos relevantes</span> en este mensaje.
                </p>
              ) : (
                <>
                  {(ext?.finance ?? []).map((f, i) => (
                    <ExtractionRow
                      key={`f${i}`}
                      icon={DollarSign}
                      tone="text-chart-3"
                      title={`${str(f.direction)} ${fmtMoney(f.amount, f.currency)}${(f.counterparty || f.place) ? ` · ${str(f.counterparty || f.place)}` : ""}${f.occurred_at ? ` · ${formatDateOnly(String(f.occurred_at))}` : ""}`}
                      tag={str(f.category)}
                      sub={str(f.description)}
                      evidence={str(f.evidence)}
                    />
                  ))}
                  {(ext?.calendar ?? []).map((c, i) => (
                    <ExtractionRow
                      key={`c${i}`}
                      icon={CalendarDays}
                      tone="text-chart-4"
                      title={`${str(c.title)} · ${str(c.starts_on)}${c.start_time ? ` ${str(c.start_time)}` : ""}${c.location ? ` · ${str(c.location)}` : ""}`}
                      evidence={str(c.evidence)}
                    />
                  ))}
                  {(ext?.hackathones ?? []).map((h, i) => (
                    <ExtractionRow
                      key={`h${i}`}
                      icon={Trophy}
                      tone="text-chart-5"
                      title={`${str(h.name)}${h.starts_on ? ` · ${str(h.starts_on)}` : ""}${h.location ? ` · ${str(h.location)}` : ""}`}
                      tag={h.modality && h.modality !== "desconocido" ? str(h.modality) : undefined}
                      sub={str(h.prizes)}
                      evidence={str(h.evidence)}
                    />
                  ))}
                  {(ext?.identidades ?? []).map((p, i) => (
                    <ExtractionRow
                      key={`id${i}`}
                      icon={Users}
                      tone="text-chart-2"
                      title={str(p.mentioned_name)}
                      tag={p.resolution_method ? str(p.resolution_method) : undefined}
                      sub={p.resolved_kind ? `→ ${str(p.resolved_kind)}` : str(p.mentioned_kind)}
                      evidence={str(p.evidence)}
                    />
                  ))}
                </>
              )}
              <PhaseButton label="Rehacer" icon={RotateCw} variant="outline" busy={busy === "extract"} disabled={busy !== null} onClick={() => extract(true)} />
            </div>
          ) : (
            <PhaseButton label="Extraer" icon={Sparkles} busy={busy === "extract"} disabled={busy !== null} onClick={() => extract(false)} />
          )}
        </Stage>

          </div>
          {/* Columna derecha: traza LLM (route+extract) + "qué hizo cada módulo" (dedup, contraparte,
              consolidación + operaciones posteriores LLM con su costo). */}
          <div className="min-w-0 space-y-4 border-t border-border pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-1">
            {trace ? (
              <TraceTree nodes={trace} />
            ) : (
              /* FALLBACK (borrar con trace_nodes): traza heurística por corridas + estado interno por módulo. */
              <>
                {llm && llm.calls > 0 ? (
                  <LlmTrace llm={llm} runs={runs} />
                ) : (
                  <p className="num text-[11px] text-muted-foreground">
                    Sin traza LLM atribuida a este mensaje (las ops internas, si las hubo, van abajo).
                  </p>
                )}
                <ModuleDebug debug={row.extractionDebug} />
              </>
            )}
          </div>
        </div>
      </PanelBody>
    </Panel>
  )
}

function fmtMoney(amount: unknown, currency: unknown): string {
  const n = Number(amount)
  const cur = String(currency ?? "").toUpperCase()
  if (!Number.isFinite(n)) return `${str(amount)} ${str(currency)}`.trim()
  try {
    return new Intl.NumberFormat("es-CO", { style: "currency", currency: cur }).format(n)
  } catch {
    return `${n.toLocaleString("es-CO")}${cur ? ` ${cur}` : ""}`
  }
}

function Stage({
  icon: Icon,
  title,
  hint,
  done,
  blocked,
  at,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  hint: string
  done?: boolean
  blocked?: boolean
  /** Cuándo corrió la fase vigente (de la traza LLM): "última corrida: hace X". */
  at?: string | null
  children: React.ReactNode
}) {
  return (
    <div className={cn("flex gap-3 py-3", blocked && "opacity-60")}>
      <div
        className={cn(
          "mt-0.5 grid size-7 shrink-0 place-items-center rounded-full border",
          done ? "border-status-ok/40 text-status-ok" : "border-border text-muted-foreground",
        )}
      >
        <Icon className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{title}</span>
          <span className="eyebrow">{hint}</span>
          <div className="num ml-auto flex items-center gap-2 text-[10px] text-muted-foreground">
            {at && (
              <span>
                última corrida <RelativeTime date={at} />
              </span>
            )}
            {done && <span className="text-status-ok">✓ hecho</span>}
          </div>
        </div>
        <div className="mt-2">{children}</div>
      </div>
    </div>
  )
}

function PhaseButton({
  label,
  icon: Icon,
  variant,
  busy,
  disabled,
  onClick,
}: {
  label: string
  icon?: React.ComponentType<{ className?: string }>
  variant?: "outline"
  busy: boolean
  disabled: boolean
  onClick: () => void
}) {
  const Ico = Icon ?? Sparkles
  return (
    <Button size="sm" variant={variant} disabled={disabled} onClick={onClick}>
      {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Ico className="size-3.5" />}
      {label}
    </Button>
  )
}

function ExtractionRow({
  icon: Icon,
  tone,
  title,
  tag,
  sub,
  evidence,
}: {
  icon: React.ComponentType<{ className?: string }>
  tone: string
  title: string
  tag?: string
  /** Línea secundaria (p. ej. la descripción/nombre del gasto). */
  sub?: string
  evidence: string
}) {
  return (
    <div className="rounded-md border border-border bg-muted/20 p-2.5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Icon className={cn("size-3.5 shrink-0", tone)} />
        <span className="font-medium">{title}</span>
        {tag && (
          <span className="num rounded-full border border-chart-3/40 bg-chart-3/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-chart-3">
            {tag}
          </span>
        )}
      </div>
      {sub && <p className="mt-1 pl-5 text-xs text-muted-foreground">{sub}</p>}
      {evidence && <p className="mt-1 pl-5 text-xs italic text-muted-foreground">“{evidence}”</p>}
    </div>
  )
}

/** Slugs que REALMENTE extrajeron, leídos de la TRAZA: `extract_grouped.slugs` o el sufijo de
 *  `extract_<slug>`. Es la verdad de "qué corrió" (vs `ext.modules` = considerados). */
function extractedSlugsFromCalls(calls: InboxLlmCall[]): string[] {
  const out = new Set<string>()
  for (const c of calls) {
    if (c.purpose === "extract_grouped") {
      const slugs = c.metadata?.slugs
      if (Array.isArray(slugs)) slugs.forEach((s) => out.add(String(s)))
    } else if (c.purpose.startsWith("extract_")) {
      out.add(c.purpose.slice("extract_".length))
    }
  }
  return [...out]
}

/** Forma de la extracción: 1 llamada agrupada (default), split en N agrupadas, o per-módulo. */
function describeExtractionCalls(calls: InboxLlmCall[]): string {
  if (calls.length === 0) return ""
  const grouped = calls.filter((c) => c.purpose === "extract_grouped").length
  const perModule = calls.length - grouped
  if (grouped > 0 && perModule === 0)
    return calls.length === 1
      ? "1 llamada agrupada (todos los módulos juntos)"
      : `${calls.length} llamadas agrupadas (split por tamaño/dependencias)`
  if (grouped === 0 && perModule > 0) return `${perModule} llamada(s) por módulo (per_module)`
  return `${calls.length} llamadas (mixto)`
}

function dedupStatusTone(status: string): string {
  if (status === "confirmed") return "text-status-ok"
  if (status === "rejected") return "text-muted-foreground"
  return "text-chart-4" // candidate (pendiente de decidir)
}

const OUTCOME_TONE: Record<string, string> = {
  unique: "text-status-ok",
  duplicate: "text-chart-4",
  pending: "text-muted-foreground",
}

interface DedupPair {
  other: string
  reason: string
  score: number | null
  status: string
  decided_by: string | null
  confidence: number | null
}

function DedupPairs({ pairs }: { pairs: DedupPair[] }) {
  if (pairs.length === 0) return null
  return (
    <div className="num mt-1 space-y-0.5 pl-2 text-[10px] text-muted-foreground">
      {pairs.map((p, i) => (
        <div key={i} className="flex flex-wrap items-center gap-x-1.5">
          <span className="text-muted-foreground/70">dedup vs {p.other}:</span>
          <span className={dedupStatusTone(p.status)}>{p.status}</span>
          {p.score != null && <span>· score {p.score.toFixed(2)}</span>}
          <span>· {p.decided_by === "llm" ? "LLM" : "proc"}</span>
          {p.confidence != null && <span>· conf {p.confidence.toFixed(2)}</span>}
          {p.reason && <span className="text-muted-foreground/60">· {p.reason}</span>}
        </div>
      ))}
    </div>
  )
}

function FinanceDebug({ rows }: { rows: FinanceDebugRow[] }) {
  return (
    <div className="space-y-1.5">
      <div className="eyebrow">finanzas · {rows.length} transacción(es)</div>
      {rows.map((r) => (
        <div key={r.transaction_id} className="num rounded border border-border bg-card/40 p-2 text-[11px]">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-foreground">tx #{r.transaction_id}</span>
            <span className="text-muted-foreground">
              {r.direction} {r.amount} {r.currency}
            </span>
            <span className={OUTCOME_TONE[r.processing_outcome] ?? "text-muted-foreground"}>
              {r.processing_outcome}
            </span>
            {r.consolidated_id != null && (
              <span className="text-muted-foreground">
                · consolidado #{r.consolidated_id}
                {r.is_winner ? " (ganadora)" : ""}
              </span>
            )}
          </div>
          <div className="mt-0.5 text-muted-foreground">
            contraparte: {r.counterparty || "—"}{" "}
            {r.counterparty_identity_id != null ? (
              <span className="text-status-ok">
                → identidad «{r.counterparty_identity_name ?? r.counterparty_identity_id}» (#
                {r.counterparty_identity_id})
              </span>
            ) : (
              <span>· sin identidad resuelta</span>
            )}
          </div>
          <DedupPairs
            pairs={r.dedup_candidates.map((c) => ({
              other: `tx #${c.other_transaction_id}`,
              reason: c.reason,
              score: c.score,
              status: c.status,
              decided_by: c.decided_by,
              confidence: c.confidence,
            }))}
          />
        </div>
      ))}
    </div>
  )
}

function IdentidadesDebug({ rows }: { rows: IdentidadesDebugRow[] }) {
  return (
    <div className="space-y-1.5">
      <div className="eyebrow">identidades · {rows.length} mención(es)</div>
      {rows.map((r) => (
        <div key={r.mention_id} className="num rounded border border-border bg-card/40 p-2 text-[11px]">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-foreground">«{r.mentioned_name}»</span>
            {r.resolved_identity_id != null ? (
              <span className="text-status-ok">
                → «{r.resolved_identity_name ?? r.resolved_identity_id}» (#{r.resolved_identity_id})
              </span>
            ) : (
              <span className="text-muted-foreground">· sin resolver</span>
            )}
            {r.resolution_method && (
              <span className="text-muted-foreground">· por {r.resolution_method}</span>
            )}
          </div>
          <DedupPairs
            pairs={r.merge_candidates.map((c) => ({
              other: `«${c.other_identity_name ?? c.other_identity_id}»`,
              reason: c.reason,
              score: c.score,
              status: c.status,
              decided_by: c.decided_by,
              confidence: c.confidence,
            }))}
          />
        </div>
      ))}
    </div>
  )
}

const INTERNAL_PURPOSE_LABEL: Record<string, string> = {
  finance_dedup: "Dedup finanzas",
  identidades_dedup: "Dedup identidades",
  identidades_cooccurrence: "Co-ocurrencia identidades",
  identidades_hierarchy: "Jerarquía identidades",
}

/** Llamadas LLM INTERNAS (dedup fase-2 / co-ocurrencia) correlacionadas a este correo, con su costo
 *  real — corren en batch con inbox_id=NULL, así que no salen en la traza principal de arriba. */
function InternalCalls({ calls }: { calls: InternalLlmCall[] }) {
  if (calls.length === 0) return null
  const total = calls.reduce((a, c) => a + c.cost_usd, 0)
  return (
    <div className="space-y-1">
      <div className="eyebrow">
        operaciones posteriores (LLM) · {calls.length} · <span className="text-brand">{fmtCost(total)}</span>
      </div>
      {calls.map((c, i) => (
        <div
          key={i}
          className="num flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded border border-border bg-card/40 p-2 text-[10px] text-muted-foreground"
        >
          <span className="text-foreground">{INTERNAL_PURPOSE_LABEL[c.purpose] ?? c.purpose}</span>
          <span>{c.model}</span>
          <span>
            {c.prompt_tokens}+{c.completion_tokens} tok
          </span>
          <span>{c.latency_ms} ms</span>
          <span className={c.status === "ok" ? "text-status-ok" : "text-status-error"}>{c.status}</span>
          {c.created_at && <RelativeTime date={c.created_at} />}
          <span className="ml-auto text-brand">{fmtCost(c.cost_usd)}</span>
        </div>
      ))}
    </div>
  )
}

/** Estado INTERNO por-módulo (capacidad `debug_inbox`): dedup, seam contraparte→identidad,
 *  consolidación + las operaciones LLM posteriores. Colapsable + JSON crudo (herramienta de debug). */
function ModuleDebug({ debug }: { debug?: ExtractionDebug | null }) {
  const [open, setOpen] = useState(true)
  const [raw, setRaw] = useState(false)
  const finance = debug?.finance?.rows ?? []
  const identidades = debug?.identidades?.rows ?? []
  const internalCalls = [
    ...(debug?.finance?.internal_calls ?? []),
    ...(debug?.identidades?.internal_calls ?? []),
  ]
  if (finance.length === 0 && identidades.length === 0 && internalCalls.length === 0) return null
  return (
    <div className="rounded-md border border-border bg-muted/10 p-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="eyebrow flex items-center gap-1.5 hover:text-foreground"
      >
        <Braces className="size-3" /> qué hizo cada módulo · dedup, contraparte, consolidación{" "}
        {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="mt-2 space-y-3">
          {finance.length > 0 && <FinanceDebug rows={finance} />}
          {identidades.length > 0 && <IdentidadesDebug rows={identidades} />}
          <InternalCalls calls={internalCalls} />
          <button
            type="button"
            onClick={() => setRaw((v) => !v)}
            className="eyebrow inline-flex items-center gap-1 hover:text-foreground"
          >
            <Braces className="size-2.5" /> JSON crudo {raw ? "▾" : "▸"}
          </button>
          {raw && (
            <pre className="num max-h-60 overflow-auto rounded border border-border bg-muted/30 p-2 text-[10px] text-muted-foreground">
              {JSON.stringify(debug, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

function MockDetail({ journey }: { journey: MessageJourney }) {
  const [raw, setRaw] = useState(false)
  const { row, steps, logs, related } = journey
  const src = SOURCE_BY_ID[row.sourceId]
  const rendered = renderPayload(row.payload, row.ocrText ?? "")
  const st = statusOf(row)
  const reprocessSteps = reprocessStepsFor(journey)

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <BackLink />
        <span className="eyebrow">camino de decisión · demo</span>
      </div>

      <Panel className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="num text-sm text-muted-foreground">inbox #{row.id}</span>
              <span className="text-sm font-semibold text-origin-inbox">{src?.name ?? row.sourceId}</span>
              <StatusBadge tone={st.tone} label={st.label} />
            </div>
            <div className="num mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
              <span>{row.externalId}</span>
              <span>
                occurred <RelativeTime date={row.occurredAt} />
              </span>
              <span>
                received <RelativeTime date={row.receivedAt} />
              </span>
              {row.attempts > 0 && <span className="text-status-error">{row.attempts} intentos</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <ReprocessButton inboxId={row.id} steps={reprocessSteps} />
            <Label htmlFor="raw-detail" className="eyebrow cursor-pointer">
              JSON crudo
            </Label>
            <Switch id="raw-detail" checked={raw} onCheckedChange={setRaw} />
          </div>
        </div>
        {raw ? (
          <pre className="mt-3 max-h-64 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-[11px] text-muted-foreground">
            {JSON.stringify(row.payload, null, 2)}
          </pre>
        ) : (
          <div className="mt-3 rounded-md border border-border bg-muted/20 p-3 text-sm">
            {rendered.sender && <div className="mb-1 font-medium">{rendered.sender}</div>}
            <p className="whitespace-pre-wrap text-muted-foreground">{rendered.body || "(sin texto)"}</p>
          </div>
        )}
      </Panel>

      <div className="grid gap-5 xl:grid-cols-[1.5fr_1fr]">
        <div>
          <div className="eyebrow mb-3">camino de decisión · {steps.length} etapas</div>
          <JourneyTimeline steps={steps} />
        </div>
        <div className="space-y-5">
          <RelatedData related={related} />
          <Panel className="overflow-hidden">
            <PanelHeader
              eyebrow="logs correlacionados"
              title="Eventos de esta request"
              sub={`request_id compartido · ${logs.length} eventos structlog`}
            />
            <div className="max-h-[380px] overflow-y-auto">
              {logs.map((l) => (
                <LogRow key={l.id} event={l} />
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}
