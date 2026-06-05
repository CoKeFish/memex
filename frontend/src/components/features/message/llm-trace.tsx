// Traza LLM como vista de logs por corridas: agrupa las llamadas de un mismo "Procesar", muestra la
// más reciente arriba, resaltada, y marca cuál dejó el resumen/extracción vigentes. Mantiene la
// estética consola (mono, LEDs, tokens de color) y permite auditar cada paso. La lógica de agrupado
// vive en ./llm-trace-runs (función pura).

import { useMemo, useState } from "react"
import {
  AlertTriangle,
  Braces,
  CalendarDays,
  ChevronDown,
  ChevronsDownUp,
  ChevronsUpDown,
  DollarSign,
  Route,
  ScanText,
  ScrollText,
  Sparkles,
  type LucideIcon,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatCompact, formatDurationMs } from "@/lib/format"
import type { InboxLlmCall, InboxLlmUsage } from "@/types/domain"
import { callDetail, fmtCost, type Run, type RunPhase } from "./llm-trace-runs"

const PURPOSE_LABEL: Record<string, string> = {
  summarize_batch: "Resumen (lote)",
  summarize_individual: "Resumen (individual)",
  module_route: "Ruteo de módulos",
  extract_finance: "Extracción · finanzas",
  extract_calendar: "Extracción · calendario",
  extract_hackathones: "Extracción · hackatones",
  extract_identidades: "Extracción · identidades",
  extract_grouped: "Extracción agrupada",
  calendar_merge: "Calendario · consolidación",
  calendar_dedup: "Calendario · dedup",
  ocr: "OCR · visión",
}

// Mapa de íconos por "tipo" de llamada. Se accede por índice (no es una llamada que cree un
// componente en render) para no disparar react-hooks/static-components.
const CALL_ICON: Record<string, LucideIcon> = {
  route: Route,
  finance: DollarSign,
  calendar: CalendarDays,
  extract: Sparkles,
  summarize: ScrollText,
  ocr: ScanText,
  other: Braces,
}

function iconKey(purpose: string): string {
  if (purpose === "module_route") return "route"
  if (purpose === "extract_finance") return "finance"
  if (purpose === "extract_calendar" || purpose.startsWith("calendar")) return "calendar"
  if (purpose.startsWith("extract")) return "extract"
  if (purpose.startsWith("summarize")) return "summarize"
  if (purpose === "ocr") return "ocr"
  return "other"
}

const PHASE_META: Record<RunPhase, { label: string; icon: LucideIcon; tone: string }> = {
  summarize: { label: "Resumen", icon: ScrollText, tone: "text-chart-2" },
  extract: { label: "Extracción", icon: Sparkles, tone: "text-chart-3" },
  calendar: { label: "Calendario", icon: CalendarDays, tone: "text-chart-4" },
  ocr: { label: "OCR / visión", icon: ScanText, tone: "text-chart-1" },
  mixed: { label: "Resumen + Extracción", icon: Sparkles, tone: "text-chart-3" },
  other: { label: "LLM", icon: Braces, tone: "text-muted-foreground" },
}

type Filter = "all" | "summarize" | "extract"

function statusClass(s: string): string {
  if (s === "ok") return "text-status-ok"
  if (s === "filtered") return "text-muted-foreground"
  return "text-status-error"
}
function statusText(s: string): string {
  return s === "filtered" ? "omitido" : s
}

function matchesFilter(r: Run, f: Filter): boolean {
  if (f === "all") return true
  if (f === "summarize") return r.phase === "summarize" || r.phase === "mixed"
  return r.phase === "extract" || r.phase === "mixed" || r.phase === "calendar"
}

function Chip({ children, accent }: { children: React.ReactNode; accent?: boolean }) {
  return (
    <span
      className={cn(
        "num rounded-full border px-1.5 py-px text-[9px] uppercase tracking-wide",
        accent ? "border-brand/40 bg-brand/10 text-brand" : "border-border text-muted-foreground",
      )}
    >
      {children}
    </span>
  )
}

function FilterButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded px-2 py-0.5 text-[11px] transition-colors",
        active ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  )
}

function CallStep({ call, index, last }: { call: InboxLlmCall; index: number; last: boolean }) {
  const [showMeta, setShowMeta] = useState(false)
  const Icon = CALL_ICON[iconKey(call.purpose)]
  const detail = callDetail(call)
  const hasMeta = call.metadata != null && Object.keys(call.metadata).length > 0
  return (
    <div className="flex gap-2.5">
      <div className="flex flex-col items-center">
        <span className="num grid size-4 shrink-0 place-items-center rounded-full border border-border text-[9px] text-muted-foreground">
          {index}
        </span>
        {!last && <span className="mt-0.5 w-px flex-1 bg-border" />}
      </div>
      <div className="min-w-0 flex-1 pb-2.5">
        <div className="num flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11px]">
          <Icon className="size-3 shrink-0 text-muted-foreground" />
          <span className="font-medium text-foreground">{PURPOSE_LABEL[call.purpose] ?? call.purpose}</span>
          <span className="text-muted-foreground">{call.model}</span>
          <span className="text-muted-foreground">
            {call.promptTokens}+{call.completionTokens} tok
          </span>
          <span className="text-muted-foreground">{formatDurationMs(call.latencyMs)}</span>
          <span className={statusClass(call.status)}>{statusText(call.status)}</span>
          {call.createdAt && <RelativeTime date={call.createdAt} className="text-muted-foreground" />}
          <span className="ml-auto text-muted-foreground">{fmtCost(call.costUsd)}</span>
        </div>
        {detail && <div className="num mt-0.5 text-[10px] text-muted-foreground">{detail}</div>}
        {call.status === "error" && call.errorMessage && (
          <div className="num mt-0.5 flex items-start gap-1 text-[10px] text-status-error">
            <AlertTriangle className="mt-px size-3 shrink-0" /> {call.errorMessage}
          </div>
        )}
        {hasMeta && (
          <button
            type="button"
            onClick={() => setShowMeta((v) => !v)}
            className="eyebrow mt-1 inline-flex items-center gap-1 hover:text-foreground"
          >
            <Braces className="size-2.5" /> metadata {showMeta ? "▾" : "▸"}
          </button>
        )}
        {showMeta && (
          <pre className="num mt-1 max-h-44 overflow-auto rounded border border-border bg-muted/30 p-2 text-[10px] text-muted-foreground">
            {JSON.stringify(call.metadata, null, 2)}
          </pre>
        )}
      </div>
    </div>
  )
}

function RunCard({ run, open, onToggle }: { run: Run; open: boolean; onToggle: () => void }) {
  const meta = PHASE_META[run.phase]
  const PhaseIcon = meta.icon
  const tokens = run.promptTokens + run.completionTokens
  return (
    <div
      className={cn(
        "rounded-md border transition-colors",
        run.isLatest ? "border-brand/30 bg-brand/[0.04]" : "border-border bg-muted/10",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-x-2.5 gap-y-1 px-2.5 py-2 text-left"
      >
        <ChevronDown className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")} />
        <Led tone={run.status === "error" ? "error" : "ok"} pulse={run.isLatest} />
        <PhaseIcon className={cn("size-3.5 shrink-0", meta.tone)} />
        <span className="text-sm font-medium">{meta.label}</span>
        {run.startedAt ? (
          <RelativeTime date={run.startedAt} className="num text-[11px] text-muted-foreground" />
        ) : (
          <span className="num text-[11px] text-muted-foreground">—</span>
        )}
        <span className="num text-[11px] text-muted-foreground">
          {run.calls.length} ll · {formatCompact(tokens)} tok · <span className="text-brand">{fmtCost(run.costUsd)}</span>
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          {run.isLatest && <Chip accent>última corrida</Chip>}
          {run.producedSummary && <Chip>→ resumen actual</Chip>}
          {run.producedExtraction && <Chip>→ extracción actual</Chip>}
        </div>
      </button>
      {open && (
        <div className="border-t border-border/60 px-2.5 pt-2.5">
          {run.calls.map((c, i) => (
            <CallStep key={i} call={c} index={i + 1} last={i === run.calls.length - 1} />
          ))}
        </div>
      )}
    </div>
  )
}

export function LlmTrace({ llm, runs }: { llm: InboxLlmUsage; runs: Run[] }) {
  const [filter, setFilter] = useState<Filter>("all")
  // openKeys === null ⇒ comportamiento por defecto (solo la última corrida abierta).
  const [openKeys, setOpenKeys] = useState<Set<string> | null>(null)

  const ordered = useMemo(() => [...runs].reverse(), [runs]) // más reciente arriba
  const shown = ordered.filter((r) => matchesFilter(r, filter))

  const isOpen = (r: Run) => (openKeys ? openKeys.has(r.key) : r.isLatest)
  const allOpen = shown.length > 0 && shown.every(isOpen)

  const toggleRun = (r: Run) =>
    setOpenKeys((prev) => {
      const base = prev ?? new Set(runs.filter((x) => x.isLatest).map((x) => x.key))
      const next = new Set(base)
      if (next.has(r.key)) next.delete(r.key)
      else next.add(r.key)
      return next
    })
  const toggleAll = () => setOpenKeys(allOpen ? new Set() : new Set(runs.map((r) => r.key)))

  const tokens = llm.promptTokens + llm.completionTokens

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="eyebrow">traza de eventos</span>
        <span className="num text-xs text-muted-foreground">
          {llm.calls} llamadas · {formatCompact(tokens)} tok ·{" "}
          <span className="text-brand">{fmtCost(llm.costUsd)}</span> · {runs.length} corrida(s)
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
            <FilterButton active={filter === "all"} onClick={() => setFilter("all")}>
              Todas
            </FilterButton>
            <FilterButton active={filter === "summarize"} onClick={() => setFilter("summarize")}>
              Resumen
            </FilterButton>
            <FilterButton active={filter === "extract"} onClick={() => setFilter("extract")}>
              Extracción
            </FilterButton>
          </div>
          <button
            type="button"
            onClick={toggleAll}
            title={allOpen ? "Colapsar todo" : "Expandir todo"}
            className="grid size-7 place-items-center rounded-md border border-border text-muted-foreground hover:text-foreground"
          >
            {allOpen ? <ChevronsDownUp className="size-3.5" /> : <ChevronsUpDown className="size-3.5" />}
          </button>
        </div>
      </div>
      {shown.length === 0 ? (
        <p className="num text-[11px] text-muted-foreground">sin corridas para este filtro.</p>
      ) : (
        <div className="space-y-1.5">
          {shown.map((r) => (
            <RunCard key={r.key} run={r} open={isOpen(r)} onToggle={() => toggleRun(r)} />
          ))}
        </div>
      )}
    </div>
  )
}
