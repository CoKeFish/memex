import { useMemo, useState } from "react"
import { Hammer, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { buildGraph, fetchGraph } from "@/data"
import type { GraphData, GraphEdge, GraphNode } from "@/data/graph"
import { useAsync } from "@/lib/use-async"

const KIND_COLOR: Record<string, string> = {
  gasto: "#10b981",
  evento: "#3b82f6",
  hackaton: "#a855f7",
  persona: "#14b8a6",
  organizacion: "#f97316",
}
const KIND_LABEL: Record<string, string> = {
  gasto: "Gasto",
  evento: "Evento",
  hackaton: "Hackatón",
  persona: "Persona",
  organizacion: "Organización",
}
const kindColor = (k: string): string => KIND_COLOR[k] ?? "#64748b"
const nodeKey = (slug: string, id: number): string => `${slug}#${id}`

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "todas", label: "Todas" },
  { value: "confirmed", label: "Reales" },
  { value: "pista", label: "Pistas" },
]

interface Sim {
  key: string
  node: GraphNode
  x: number
  y: number
  vx: number
  vy: number
  deg: number
}

interface Positioned {
  sims: Sim[]
  byKey: Map<string, Sim>
  viewBox: string
}

/** Layout determinista por simulación de fuerzas (repulsión + resortes + centrado). Sin librerías:
 *  posiciones iniciales en círculo (sin aleatoriedad) → mismo grafo, mismo dibujo. */
function layout(nodes: GraphNode[], edges: GraphEdge[]): Positioned {
  const sims: Sim[] = nodes.map((node, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, nodes.length)
    return { key: nodeKey(node.slug, node.id), node, x: Math.cos(a) * 220, y: Math.sin(a) * 220, vx: 0, vy: 0, deg: 0 }
  })
  const byKey = new Map(sims.map((s) => [s.key, s]))
  const links: [Sim, Sim][] = []
  for (const e of edges) {
    const a = byKey.get(nodeKey(e.srcSlug, e.srcId))
    const b = byKey.get(nodeKey(e.dstSlug, e.dstId))
    if (a && b) {
      links.push([a, b])
      a.deg += 1
      b.deg += 1
    }
  }

  const REP = 11000
  const SPRING = 0.02
  const REST = 95
  const CENTER = 0.006
  const DAMP = 0.85
  const DT = 0.85
  for (let iter = 0; iter < 300; iter++) {
    for (let i = 0; i < sims.length; i++) {
      for (let j = i + 1; j < sims.length; j++) {
        const a = sims[i]
        const b = sims[j]
        let dx = a.x - b.x
        let dy = a.y - b.y
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) {
          d2 = 0.01
          dx = 0.1
          dy = 0.1
        }
        const d = Math.sqrt(d2)
        const f = REP / d2
        const fx = (dx / d) * f
        const fy = (dy / d) * f
        a.vx += fx
        a.vy += fy
        b.vx -= fx
        b.vy -= fy
      }
    }
    for (const [a, b] of links) {
      const dx = b.x - a.x
      const dy = b.y - a.y
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01
      const f = (d - REST) * SPRING
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      a.vx += fx
      a.vy += fy
      b.vx -= fx
      b.vy -= fy
    }
    for (const s of sims) {
      s.vx -= s.x * CENTER
      s.vy -= s.y * CENTER
      s.x += s.vx * DT
      s.y += s.vy * DT
      s.vx *= DAMP
      s.vy *= DAMP
    }
  }

  let minX = -100
  let minY = -100
  let maxX = 100
  let maxY = 100
  for (const s of sims) {
    minX = Math.min(minX, s.x)
    minY = Math.min(minY, s.y)
    maxX = Math.max(maxX, s.x)
    maxY = Math.max(maxY, s.y)
  }
  const pad = 70
  const vb = `${minX - pad} ${minY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`
  return { sims, byKey, viewBox: vb }
}

function edgeStyle(status: string): { stroke: string; width: number; dash?: string; opacity: number } {
  if (status === "confirmed") return { stroke: "#22c55e", width: 1.8, opacity: 0.9 }
  if (status === "rejected") return { stroke: "#ef4444", width: 1.2, dash: "2 4", opacity: 0.4 }
  return { stroke: "#94a3b8", width: 1.3, dash: "5 4", opacity: 0.6 } // pista
}

function GraphCanvas({ data }: { data: GraphData }) {
  const [hover, setHover] = useState<string | null>(null)
  const { sims, byKey, viewBox } = useMemo(() => layout(data.nodes, data.edges), [data.nodes, data.edges])

  const incident = useMemo(() => {
    const set = new Set<string>()
    if (hover) {
      set.add(hover)
      for (const e of data.edges) {
        const a = nodeKey(e.srcSlug, e.srcId)
        const b = nodeKey(e.dstSlug, e.dstId)
        if (a === hover) set.add(b)
        if (b === hover) set.add(a)
      }
    }
    return set
  }, [hover, data.edges])

  const dim = (key: string): number => (hover && !incident.has(key) ? 0.18 : 1)

  return (
    <svg viewBox={viewBox} className="w-full" style={{ height: 620 }} role="img" aria-label="Grafo de relaciones">
      {data.edges.map((e) => {
        const a = byKey.get(nodeKey(e.srcSlug, e.srcId))
        const b = byKey.get(nodeKey(e.dstSlug, e.dstId))
        if (!a || !b) return null
        const st = edgeStyle(e.status)
        const lit = !hover || (incident.has(a.key) && incident.has(b.key))
        return (
          <line
            key={e.id}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            stroke={st.stroke}
            strokeWidth={st.width}
            strokeDasharray={st.dash}
            opacity={lit ? st.opacity : 0.08}
          >
            <title>{`${e.relationType || "—"} · ${e.status} · ${e.producer}`}</title>
          </line>
        )
      })}
      {sims.map((s) => {
        const r = 6 + Math.min(s.deg, 10) * 0.5
        const o = dim(s.key)
        return (
          <g
            key={s.key}
            transform={`translate(${s.x} ${s.y})`}
            opacity={o}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
            style={{ cursor: "pointer" }}
          >
            <circle r={r} fill={kindColor(s.node.kind)} stroke="white" strokeWidth={1.5} />
            <text
              y={r + 11}
              textAnchor="middle"
              fontSize={10}
              className="fill-foreground"
              style={{ pointerEvents: "none" }}
            >
              {s.node.label.length > 22 ? `${s.node.label.slice(0, 21)}…` : s.node.label}
            </text>
            <title>{`${s.node.label} · ${KIND_LABEL[s.node.kind] ?? s.node.kind}`}</title>
          </g>
        )
      })}
    </svg>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
      {Object.entries(KIND_LABEL).map(([k, label]) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2.5 rounded-full" style={{ background: kindColor(k) }} />
          {label}
        </span>
      ))}
      <span className="ml-2 inline-flex items-center gap-1.5">
        <svg width="22" height="6">
          <line x1="0" y1="3" x2="22" y2="3" stroke="#22c55e" strokeWidth="1.8" />
        </svg>
        Real
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width="22" height="6">
          <line x1="0" y1="3" x2="22" y2="3" stroke="#94a3b8" strokeWidth="1.3" strokeDasharray="5 4" />
        </svg>
        Pista
      </span>
    </div>
  )
}

export function GraphPage() {
  const [status, setStatus] = useState<string>("todas")
  const [building, setBuilding] = useState(false)
  const { data, loading, error, reload } = useAsync<GraphData>(
    () => fetchGraph(status === "todas" ? undefined : status),
    [status],
  )
  const graph = data ?? { nodes: [], edges: [] }

  async function onBuild() {
    setBuilding(true)
    try {
      const r = await buildGraph()
      toast.success(
        `Grafo armado: ${r.cooccurrencePistas} pistas, ${r.afiliacionReales} reales` +
          (r.highFanoutSkipped ? ` · ${r.highFanoutSkipped} mensajes saltados` : ""),
      )
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "No se pudo armar el grafo")
    } finally {
      setBuilding(false)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="relaciones · grafo"
        title="Grafo de relaciones"
        description="Un solo grafo con todos tus datos. Cada vértice es una entidad única (gasto, evento, hackatón, persona, organización); las aristas las forman el inbox (co-ocurrencia = PISTA, sin vouchar) y el directorio (afiliación = REAL). El LLM valida las pistas después. inbox no es vértice: es la procedencia."
        actions={
          <div className="flex items-center gap-2">
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="h-8 w-auto min-w-[100px] text-xs" aria-label="Nivel de arista">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value} className="text-xs">
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button size="sm" variant="outline" onClick={onBuild} disabled={building}>
              {building ? <Loader2 className="size-4 animate-spin" /> : <Hammer className="size-4" />}
              Armar grafo
            </Button>
          </div>
        }
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando grafo…
        </div>
      ) : graph.nodes.length === 0 ? (
        <EmptyState
          title="Grafo vacío"
          hint="Todavía no hay vértices. Corré la extracción en Procesamiento; cuando haya datos, tocá «Armar grafo» para materializar las aristas deterministas."
        />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-xs text-muted-foreground">
              {graph.nodes.length} vértices · {graph.edges.length} aristas
            </div>
            <Legend />
          </div>
          <div className="overflow-hidden rounded-lg border bg-muted/20">
            <GraphCanvas data={graph} />
          </div>
        </div>
      )}
    </div>
  )
}
