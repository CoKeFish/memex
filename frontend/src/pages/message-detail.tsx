import { useState } from "react"
import { ArrowLeft, CalendarDays, DollarSign, Eye, Loader2, RotateCw, ScrollText, Sparkles, Zap } from "lucide-react"
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
import { RelatedData } from "@/components/features/message/related-data"
import { ReprocessButton, reprocessStepsFor } from "@/components/features/message/reprocess-button"
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
import type { Tone } from "@/lib/status"
import type { InboxLlmCall, InboxLlmUsage, InboxRow, MessageJourney, Source } from "@/types/domain"

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
  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const source = (sources ?? []).find((s) => s.id === row.sourceId)
  const meta = sourceMeta(source)
  const SrcIcon = meta.icon
  const rendered = renderPayload(row.payload, row.ocrText ?? "")
  const cls = row.classification

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
          <div className="flex items-center gap-2">
            <Label htmlFor="raw-detail" className="eyebrow cursor-pointer">JSON crudo</Label>
            <Switch id="raw-detail" checked={raw} onCheckedChange={setRaw} />
          </div>
        </div>
        {raw ? (
          <pre className="mt-3 max-h-80 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-[11px] text-muted-foreground">
            {JSON.stringify(row.payload, null, 2)}
          </pre>
        ) : (
          <div className="mt-3 rounded-md border border-border bg-muted/20 p-3 text-sm">
            {rendered.sender && <div className="mb-1 font-medium">{rendered.sender}</div>}
            <p className="whitespace-pre-wrap text-muted-foreground">{rendered.body || "(sin texto)"}</p>
          </div>
        )}
      </Panel>

      <PipelinePanel row={row} onProcessed={onProcessed} />
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
  const extDone = !!ext?.done
  const extItems = (ext?.finance.length ?? 0) + (ext?.calendar.length ?? 0)
  const input = renderPayload(row.payload, row.ocrText ?? "")

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
      const n = (r.finance?.length ?? 0) + (r.calendar?.length ?? 0)
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
          <div className="flex items-center gap-1 rounded-md border border-border p-0.5">
            {(["individual", "window"] as ProcessScope[]).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setScope(s)}
                className={cn(
                  "rounded px-2 py-0.5 text-[11px] transition-colors",
                  scope === s ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {s === "individual" ? "Solo este" : "Su lote"}
              </button>
            ))}
          </div>
        }
      />
      <PanelBody className="divide-y divide-border py-0">
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
        <Stage icon={ScrollText} title="Resumen" hint="LLM" done={!!summary} blocked={!cls}>
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
        <Stage icon={Sparkles} title="Extracción" hint="LLM · finanzas/calendario" done={extDone} blocked={!cls}>
          {!cls ? (
            <span className="text-xs text-muted-foreground">Clasificá primero.</span>
          ) : extDone ? (
            <div className="space-y-2">
              {ext && ext.modules.length > 0 && (
                <div className="eyebrow">módulos corridos: {ext.modules.join(", ")}</div>
              )}
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
                      title={`${fmtMoney(f.amount, f.currency)}${f.merchant ? ` · ${str(f.merchant)}` : ""}${f.occurred_on ? ` · ${str(f.occurred_on)}` : ""}`}
                      tag={str(f.category)}
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
                </>
              )}
              <PhaseButton label="Rehacer" icon={RotateCw} variant="outline" busy={busy === "extract"} disabled={busy !== null} onClick={() => extract(true)} />
            </div>
          ) : (
            <PhaseButton label="Extraer" icon={Sparkles} busy={busy === "extract"} disabled={busy !== null} onClick={() => extract(false)} />
          )}
        </Stage>

        {/* Traza LLM — auditoría: cada llamada con modelo, tokens, latencia, costo */}
        {llm && llm.calls > 0 && (
          <div className="py-3">
            <LlmTrace llm={llm} />
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

function LlmTrace({ llm }: { llm: InboxLlmUsage }) {
  const PURPOSE: Record<string, string> = {
    summarize_batch: "Resumen (lote)",
    summarize_individual: "Resumen (individual)",
    module_route: "Ruteo de módulos",
    extract_finance: "Extracción · finanzas",
    extract_calendar: "Extracción · calendario",
    extract_grouped: "Extracción agrupada",
  }
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="eyebrow">traza llm</span>
        <span className="num text-xs text-muted-foreground">
          {llm.calls} llamada(s) · {llm.promptTokens + llm.completionTokens} tokens ·{" "}
          <span className="text-brand">{fmtCost(llm.costUsd)}</span>
        </span>
      </div>
      <div className="space-y-1">
        {llm.items.map((c, i) => {
          const detail = callDetail(c)
          return (
            <div key={i} className="rounded-md border border-border bg-muted/20 px-2.5 py-1.5">
              <div className="num flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]">
                <span className="font-medium text-foreground">{PURPOSE[c.purpose] ?? c.purpose}</span>
                <span className="text-muted-foreground">{c.model}</span>
                <span className="text-muted-foreground">
                  {c.promptTokens}+{c.completionTokens} tok
                </span>
                <span className="text-muted-foreground">{c.latencyMs}ms</span>
                <span className={c.status === "ok" ? "text-status-ok" : "text-status-error"}>
                  {c.status}
                </span>
                <span className="ml-auto text-muted-foreground">{fmtCost(c.costUsd)}</span>
              </div>
              {detail && <div className="num mt-1 text-[10px] text-muted-foreground">{detail}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** Resume la decisión de una fase desde su metadata (auditoría del ruteo / extracción). */
function callDetail(c: InboxLlmCall): string {
  const m = c.metadata ?? {}
  const list = (v: unknown) => (Array.isArray(v) ? v.map(String).join(", ") : str(v))
  if (c.purpose === "module_route") {
    const chosen = list(m.chosen) || "ninguno"
    return `evaluó: ${list(m.slugs_in)} → eligió: ${chosen}`
  }
  if (c.purpose.startsWith("extract_")) {
    return `items: ${str(m.items)} · descartados: ${str(m.discarded)}${m.n ? ` · ${str(m.n)} msj en ventana` : ""}`
  }
  return ""
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

function fmtCost(usd: number): string {
  if (!usd) return "$0"
  if (usd < 0.01) return `$${usd.toFixed(6)}`
  return `$${usd.toFixed(4)}`
}

function Stage({
  icon: Icon,
  title,
  hint,
  done,
  blocked,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  hint: string
  done?: boolean
  blocked?: boolean
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
          {done && <span className="num ml-auto text-[10px] text-status-ok">✓ hecho</span>}
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
  evidence,
}: {
  icon: React.ComponentType<{ className?: string }>
  tone: string
  title: string
  tag?: string
  evidence: string
}) {
  return (
    <div className="rounded-md border border-border bg-muted/20 p-2.5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Icon className={cn("size-3.5 shrink-0", tone)} />
        <span className="font-medium">{title}</span>
        {tag && (
          <span className="rounded-full border border-border bg-background px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            {tag}
          </span>
        )}
      </div>
      {evidence && <p className="mt-1 pl-5 text-xs italic text-muted-foreground">“{evidence}”</p>}
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
