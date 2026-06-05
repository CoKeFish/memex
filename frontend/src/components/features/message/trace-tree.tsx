// Árbol de traza jerárquica de un mensaje (vista en "stack"): toma la lista PLANA de nodos
// (TraceNodeDto[], con parentId) que produce el backend y la renderiza como colapsables anidados.
// Es GENÉRICO: se guía solo por `kind`/`status`/`detail`/`cost` — no sabe de finance/identidades ni
// de ningún módulo. Un módulo nuevo que emita `ctx.trace.*` aparece acá sin tocar este archivo.
// Reusa la estética consola del dashboard (mono, LEDs, tokens de color, fmtCost).

import { useMemo, useState } from "react"
import {
  Boxes,
  ChevronDown,
  ChevronsDownUp,
  ChevronsUpDown,
  CircleDot,
  Dot,
  GitBranch,
  Hash,
  ScrollText,
  Sparkles,
  type LucideIcon,
} from "lucide-react"
import { JsonView, allExpanded, darkStyles } from "react-json-view-lite"
import "react-json-view-lite/dist/index.css"
import { cn } from "@/lib/utils"
import { Led } from "@/components/common/led"
import { formatDurationMs } from "@/lib/format"
import { fmtCost } from "./llm-trace-runs"
import type { Tone } from "@/lib/status"
import type { TraceNodeDto, TraceNodeKind } from "@/types/domain"

// --- presentación por `kind` (única tabla de estilo; el resto es agnóstico) -------------------- //
const KIND_ICON: Record<TraceNodeKind, LucideIcon> = {
  root: ScrollText,
  module: Boxes,
  entity: Hash,
  step: CircleDot,
  log: Dot,
  decision: GitBranch,
  llm: Sparkles,
}
const KIND_TONE: Record<TraceNodeKind, string> = {
  root: "text-muted-foreground",
  module: "text-chart-3",
  entity: "text-chart-2",
  step: "text-muted-foreground",
  log: "text-muted-foreground",
  decision: "text-chart-4",
  llm: "text-chart-1",
}

function statusTone(s: TraceNodeDto["status"]): Tone {
  if (s === "ok") return "ok"
  if (s === "error") return "error"
  if (s === "warn") return "review"
  return "neutral" // info | null
}
function llmStatusClass(s: string): string {
  if (s === "ok") return "text-status-ok"
  if (s === "filtered") return "text-muted-foreground"
  return "text-status-error"
}

// --- árbol a partir de la lista plana ---------------------------------------------------------- //
interface TreeNode extends TraceNodeDto {
  children: TreeNode[]
}

function buildForest(nodes: TraceNodeDto[]): TreeNode[] {
  const byId = new Map<number, TreeNode>()
  for (const n of nodes) byId.set(n.id, { ...n, children: [] })
  const roots: TreeNode[] = []
  for (const n of byId.values()) {
    const parent = n.parentId != null ? byId.get(n.parentId) : undefined
    if (parent) parent.children.push(n)
    else roots.push(n)
  }
  const bySeq = (a: TreeNode, b: TreeNode) => a.seq - b.seq || a.id - b.id
  roots.sort(bySeq)
  for (const n of byId.values()) n.children.sort(bySeq)
  return roots
}

function flatten(forest: TreeNode[]): TreeNode[] {
  const out: TreeNode[] = []
  const walk = (ns: TreeNode[]) => ns.forEach((n) => (out.push(n), walk(n.children)))
  walk(forest)
  return out
}

function hasDetail(n: TraceNodeDto): boolean {
  return Object.keys(n.detail).length > 0
}
function isExpandable(n: TreeNode): boolean {
  return n.children.length > 0 || hasDetail(n) || (n.kind === "llm" && n.llm != null)
}

// --- piezas de render -------------------------------------------------------------------------- //
function fmtVal(v: unknown): string {
  if (v == null) return "—"
  if (typeof v === "object") return JSON.stringify(v)
  if (typeof v === "number") return String(v)
  return String(v)
}

function DetailKv({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail)
  if (entries.length === 0) return null
  return (
    <div className="num flex flex-wrap gap-x-3 gap-y-0.5 py-0.5 text-[10px] text-muted-foreground">
      {entries.map(([k, v]) => (
        <span key={k}>
          <span className="text-muted-foreground/60">{k}:</span> <span className="text-foreground/80">{fmtVal(v)}</span>
        </span>
      ))}
    </div>
  )
}

/** Parsea el output del LLM como JSON para mostrarlo formateado (tolera fences ```json). undefined si
 *  no es JSON (prosa, output cortado) → el caller cae al texto crudo. */
function parseJsonLoose(text: string): object | undefined {
  let s = text.trim()
  if (s.startsWith("```")) s = s.replace(/^```[a-z]*\s*/i, "").replace(/\s*```$/, "").trim()
  if (!(s.startsWith("{") || s.startsWith("["))) return undefined
  try {
    const v: unknown = JSON.parse(s)
    return typeof v === "object" && v !== null ? v : undefined
  } catch {
    return undefined
  }
}

function LlmLeaf({ llm }: { llm: NonNullable<TraceNodeDto["llm"]> }) {
  const parsed = llm.responseText ? parseJsonLoose(llm.responseText) : undefined
  return (
    <div className="space-y-1 py-0.5">
      <div className="num flex flex-wrap items-center gap-x-2 text-[10px] text-muted-foreground">
        <span className="text-foreground/80">{llm.model}</span>
        <span>
          {llm.promptTokens}+{llm.completionTokens} tok
        </span>
        <span>{formatDurationMs(llm.latencyMs)}</span>
        <span className={llmStatusClass(llm.status)}>{llm.status === "filtered" ? "omitido" : llm.status}</span>
      </div>
      {parsed !== undefined ? (
        // JSON formateado (árbol colapsable, tema oscuro). Output crudo no-JSON cae al <pre> de abajo.
        <div className="num max-h-60 overflow-auto rounded border border-border bg-muted/30 p-2 text-[11px]">
          <JsonView data={parsed} shouldExpandNode={allExpanded} style={darkStyles} />
        </div>
      ) : (
        llm.responseText && (
          <pre className="num max-h-44 overflow-auto rounded border border-border bg-muted/30 p-2 text-[10px] leading-relaxed text-muted-foreground">
            {llm.responseText}
          </pre>
        )
      )}
    </div>
  )
}

function CostTag({ cost }: { cost: TraceNodeDto["cost"] }) {
  if (cost.calls === 0 && cost.subtreeUsd === 0) return null
  return (
    <span className="num ml-auto shrink-0 pl-2 text-[10px] text-muted-foreground">
      {cost.calls > 0 && <>{cost.calls} ll · </>}
      <span className="text-brand">{fmtCost(cost.subtreeUsd)}</span>
    </span>
  )
}

function NodeRow({
  node,
  expanded,
  toggle,
}: {
  node: TreeNode
  expanded: Set<number>
  toggle: (id: number) => void
}) {
  const open = expanded.has(node.id)
  const expandable = isExpandable(node)
  const Icon = KIND_ICON[node.kind]
  return (
    <div>
      <button
        type="button"
        disabled={!expandable}
        onClick={() => toggle(node.id)}
        aria-expanded={expandable ? open : undefined}
        className={cn(
          "flex w-full items-center gap-1.5 rounded py-1 pr-1 text-left transition-colors",
          expandable ? "hover:bg-muted/30" : "cursor-default",
        )}
      >
        {expandable ? (
          <ChevronDown
            className={cn("size-3 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")}
          />
        ) : (
          <span className="size-3 shrink-0" />
        )}
        <Led tone={statusTone(node.status)} size={6} />
        <Icon className={cn("size-3 shrink-0", KIND_TONE[node.kind])} />
        <span className="truncate text-[12px] text-foreground">{node.label}</span>
        {node.ref && <span className="num shrink-0 text-[10px] text-muted-foreground">#{node.ref.id}</span>}
        {node.kind === "module" && node.moduleSlug && (
          <span className="num shrink-0 rounded-full border border-border px-1.5 py-px text-[9px] uppercase tracking-wide text-muted-foreground">
            {node.moduleSlug}
          </span>
        )}
        <CostTag cost={node.cost} />
      </button>
      {open && expandable && (
        <div className="ml-[5px] space-y-0.5 border-l border-border/50 pl-3">
          {hasDetail(node) && <DetailKv detail={node.detail} />}
          {node.kind === "llm" && node.llm && <LlmLeaf llm={node.llm} />}
          {node.children.map((c) => (
            <NodeRow key={c.id} node={c} expanded={expanded} toggle={toggle} />
          ))}
        </div>
      )}
    </div>
  )
}

export function TraceTree({ nodes }: { nodes: TraceNodeDto[] }) {
  const forest = useMemo(() => buildForest(nodes), [nodes])
  // Un único `root` por mensaje es redundante mostrarlo: rendereamos sus hijos al tope.
  const topLevel = forest.length === 1 && forest[0].kind === "root" ? forest[0].children : forest

  const all = useMemo(() => flatten(topLevel), [topLevel])
  const expandableIds = useMemo(() => all.filter(isExpandable).map((n) => n.id), [all])
  // Por defecto abrimos los spans de módulo (se ven las entidades); el resto colapsado.
  const defaultExpanded = useMemo(
    () => new Set(all.filter((n) => n.kind === "module").map((n) => n.id)),
    [all],
  )
  const [override, setOverride] = useState<Set<number> | null>(null)
  const expanded = override ?? defaultExpanded

  const allOpen = expandableIds.length > 0 && expandableIds.every((id) => expanded.has(id))
  const toggle = (id: number) =>
    setOverride(() => {
      const next = new Set(expanded)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const toggleAll = () => setOverride(allOpen ? new Set() : new Set(expandableIds))

  const totalCost = topLevel.reduce((a, n) => a + n.cost.subtreeUsd, 0)
  const totalCalls = topLevel.reduce((a, n) => a + n.cost.calls, 0)

  if (topLevel.length === 0) {
    return <p className="num text-[11px] text-muted-foreground">Sin traza para este mensaje.</p>
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="eyebrow">traza de ejecución</span>
        <span className="num text-xs text-muted-foreground">
          {totalCalls} llamada(s) · <span className="text-brand">{fmtCost(totalCost)}</span>
        </span>
        {expandableIds.length > 0 && (
          <button
            type="button"
            onClick={toggleAll}
            title={allOpen ? "Colapsar todo" : "Expandir todo"}
            className="ml-auto grid size-7 place-items-center rounded-md border border-border text-muted-foreground hover:text-foreground"
          >
            {allOpen ? <ChevronsDownUp className="size-3.5" /> : <ChevronsUpDown className="size-3.5" />}
          </button>
        )}
      </div>
      <div className="space-y-0.5">
        {topLevel.map((n) => (
          <NodeRow key={n.id} node={n} expanded={expanded} toggle={toggle} />
        ))}
      </div>
    </div>
  )
}
