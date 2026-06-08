import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { Hammer, Loader2, Maximize2 } from "lucide-react"
import { toast } from "sonner"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { buildGraph, fetchGraph } from "@/data"
import type { GraphData, GraphEdge, GraphNode } from "@/data/graph"
import { useAsync } from "@/lib/use-async"

const KIND_COLOR: Record<string, string> = {
  transaccion: "#10b981",
  evento: "#3b82f6",
  hackaton: "#a855f7",
  persona: "#14b8a6",
  organizacion: "#f97316",
  registro: "#eab308",
  habito: "#ec4899",
}
const KIND_LABEL: Record<string, string> = {
  transaccion: "Cobro/pago",
  evento: "Evento",
  hackaton: "Hackatón",
  persona: "Persona",
  organizacion: "Organización",
  registro: "Registro",
  habito: "Hábito",
}
const kindColor = (k: string): string => KIND_COLOR[k] ?? "#64748b"
const nodeKey = (slug: string, id: number): string => `${slug}#${id}`

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "todas", label: "Todas" },
  { value: "confirmed", label: "Reales" },
  { value: "pista", label: "Pistas" },
]

// Viewport interno del SVG (coordenadas fijas; el CSS lo escala responsivo).
const VW = 1000
const VH = 600
const FIT_PAD = 50

interface Sim {
  key: string
  node: GraphNode
  x: number
  y: number
  vx: number
  vy: number
  deg: number
}

interface Layout {
  sims: Sim[]
  byKey: Map<string, Sim>
  bounds: { minX: number; minY: number; maxX: number; maxY: number }
}

interface View {
  x: number
  y: number
  k: number
}

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v))

/** Layout determinista por simulación de fuerzas (repulsión + resortes + centrado). Sin librerías. */
function layout(nodes: GraphNode[], edges: GraphEdge[]): Layout {
  const sims: Sim[] = nodes.map((node, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, nodes.length)
    const radius = 60 + nodes.length * 2
    return { key: nodeKey(node.slug, node.id), node, x: Math.cos(a) * radius, y: Math.sin(a) * radius, vx: 0, vy: 0, deg: 0 }
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

  const REP = 14000
  const SPRING = 0.025
  const REST = 110
  const CENTER = 0.005
  const DAMP = 0.85
  const DT = 0.85
  const iters = sims.length > 250 ? 130 : 320
  for (let iter = 0; iter < iters; iter++) {
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
        a.vx += (dx / d) * f
        a.vy += (dy / d) * f
        b.vx -= (dx / d) * f
        b.vy -= (dy / d) * f
      }
    }
    for (const [a, b] of links) {
      const dx = b.x - a.x
      const dy = b.y - a.y
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01
      const f = (d - REST) * SPRING
      a.vx += (dx / d) * f
      a.vy += (dy / d) * f
      b.vx -= (dx / d) * f
      b.vy -= (dy / d) * f
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

  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const s of sims) {
    minX = Math.min(minX, s.x)
    minY = Math.min(minY, s.y)
    maxX = Math.max(maxX, s.x)
    maxY = Math.max(maxY, s.y)
  }
  if (!sims.length) {
    minX = -100
    minY = -100
    maxX = 100
    maxY = 100
  }
  return { sims, byKey, bounds: { minX, minY, maxX, maxY } }
}

function fitView(b: Layout["bounds"]): View {
  const w = b.maxX - b.minX || 1
  const h = b.maxY - b.minY || 1
  const k = clamp(Math.min((VW - 2 * FIT_PAD) / w, (VH - 2 * FIT_PAD) / h), 0.15, 3)
  const cx = (b.minX + b.maxX) / 2
  const cy = (b.minY + b.maxY) / 2
  return { k, x: VW / 2 - cx * k, y: VH / 2 - cy * k }
}

function edgeStyle(status: string): { stroke: string; width: number; dash?: string } {
  if (status === "confirmed") return { stroke: "#22c55e", width: 2 }
  if (status === "rejected") return { stroke: "#ef4444", width: 1.5, dash: "2 4" }
  return { stroke: "#94a3b8", width: 1.5, dash: "5 4" } // pista
}

function GraphCanvas({
  data,
  selected,
  onSelect,
}: {
  data: GraphData
  selected: string | null
  onSelect: (k: string | null) => void
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<string | null>(null)
  const lay = useMemo(() => layout(data.nodes, data.edges), [data.nodes, data.edges])
  const [view, setView] = useState<View>(() => fitView(lay.bounds))
  // Re-encuadra al cambiar el conjunto (filtro/armado), no en cada render.
  useEffect(() => setView(fitView(lay.bounds)), [lay])

  const focus = selected ?? hover
  const incident = useMemo(() => {
    const set = new Set<string>()
    if (focus) {
      set.add(focus)
      for (const e of data.edges) {
        const a = nodeKey(e.srcSlug, e.srcId)
        const b = nodeKey(e.dstSlug, e.dstId)
        if (a === focus) set.add(b)
        if (b === focus) set.add(a)
      }
    }
    return set
  }, [focus, data.edges])

  const toView = useCallback((clientX: number, clientY: number): { x: number; y: number } | null => {
    const svg = svgRef.current
    const ctm = svg?.getScreenCTM()
    if (!svg || !ctm) return null
    const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse())
    return { x: p.x, y: p.y }
  }, [])

  const onWheel = (e: React.WheelEvent) => {
    const loc = toView(e.clientX, e.clientY)
    if (!loc) return
    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18
    setView((v) => {
      const k = clamp(v.k * factor, 0.1, 9)
      const wx = (loc.x - v.x) / v.k
      const wy = (loc.y - v.y) / v.k
      return { k, x: loc.x - wx * k, y: loc.y - wy * k }
    })
  }

  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const onPointerDown = (e: React.PointerEvent) => {
    const loc = toView(e.clientX, e.clientY)
    if (!loc) return
    drag.current = { x: loc.x, y: loc.y, moved: false }
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return
    const loc = toView(e.clientX, e.clientY)
    if (!loc) return
    const dx = loc.x - drag.current.x
    const dy = loc.y - drag.current.y
    if (Math.abs(dx) + Math.abs(dy) > 1) drag.current.moved = true
    drag.current.x = loc.x
    drag.current.y = loc.y
    setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    // click en el fondo (sin arrastre) = deseleccionar
    if (drag.current && !drag.current.moved) onSelect(null)
    drag.current = null
    ;(e.target as Element).releasePointerCapture?.(e.pointerId)
  }

  const labelsAlways = lay.sims.length <= 60

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${VW} ${VH}`}
      preserveAspectRatio="xMidYMid meet"
      className="w-full touch-none select-none"
      style={{ height: 600, cursor: "grab" }}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      role="img"
      aria-label="Grafo de relaciones"
    >
      <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
        {data.edges.map((e) => {
          const a = lay.byKey.get(nodeKey(e.srcSlug, e.srcId))
          const b = lay.byKey.get(nodeKey(e.dstSlug, e.dstId))
          if (!a || !b) return null
          const st = edgeStyle(e.status)
          const lit = !focus || (incident.has(a.key) && incident.has(b.key))
          return (
            <line
              key={e.id}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={st.stroke}
              strokeWidth={st.width / view.k}
              strokeDasharray={st.dash ? st.dash.split(" ").map((n) => Number(n) / view.k).join(" ") : undefined}
              opacity={lit ? 0.85 : 0.07}
            >
              <title>{`${e.relationType || "—"} · ${e.status} · ${e.producer}`}</title>
            </line>
          )
        })}
        {lay.sims.map((s) => {
          const r = (6 + Math.min(s.deg, 10) * 0.6) / view.k
          const isSel = selected === s.key
          const dim = focus && !incident.has(s.key) ? 0.18 : 1
          return (
            <g
              key={s.key}
              transform={`translate(${s.x} ${s.y})`}
              opacity={dim}
              onPointerEnter={() => setHover(s.key)}
              onPointerLeave={() => setHover(null)}
              onClick={(ev) => {
                ev.stopPropagation()
                onSelect(isSel ? null : s.key)
              }}
              style={{ cursor: "pointer" }}
            >
              <circle
                r={r}
                fill={kindColor(s.node.kind)}
                stroke={isSel ? "#0f172a" : "white"}
                strokeWidth={(isSel ? 2.5 : 1.5) / view.k}
              />
              {(labelsAlways || focus === s.key) && (
                <text
                  y={r + 11 / view.k}
                  textAnchor="middle"
                  fontSize={10 / view.k}
                  className="fill-foreground"
                  style={{ pointerEvents: "none" }}
                >
                  {s.node.label.length > 24 ? `${s.node.label.slice(0, 23)}…` : s.node.label}
                </text>
              )}
              <title>{`${s.node.label} · ${KIND_LABEL[s.node.kind] ?? s.node.kind}`}</title>
            </g>
          )
        })}
      </g>
    </svg>
  )
}

function DetailPanel({ node, edges, nodesByKey }: { node: GraphNode; edges: GraphEdge[]; nodesByKey: Map<string, GraphNode> }) {
  const mine = edges.filter(
    (e) => nodeKey(e.srcSlug, e.srcId) === nodeKey(node.slug, node.id) || nodeKey(e.dstSlug, e.dstId) === nodeKey(node.slug, node.id),
  )
  return (
    <div className="w-full shrink-0 space-y-3 rounded-lg border bg-card p-3 text-sm md:w-72">
      <div>
        <div className="flex items-center gap-2">
          <span className="inline-block size-3 rounded-full" style={{ background: kindColor(node.kind) }} />
          <span className="text-xs uppercase tracking-wide text-muted-foreground">
            {KIND_LABEL[node.kind] ?? node.kind}
          </span>
        </div>
        <div className="mt-1 font-medium leading-tight">{node.label}</div>
        <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
          {node.slug}#{node.id}
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-muted-foreground">
          Relaciones ({mine.length})
        </div>
        {mine.length === 0 ? (
          <p className="text-xs text-muted-foreground">Sin aristas. (Las semánticas llegan con el LLM.)</p>
        ) : (
          <ul className="space-y-1.5">
            {mine.map((e) => {
              const otherKey =
                nodeKey(e.srcSlug, e.srcId) === nodeKey(node.slug, node.id)
                  ? nodeKey(e.dstSlug, e.dstId)
                  : nodeKey(e.srcSlug, e.srcId)
              const other = nodesByKey.get(otherKey)
              return (
                <li key={e.id} className="rounded border bg-muted/30 px-2 py-1 text-xs">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="inline-block h-0.5 w-4 rounded"
                      style={{ background: e.status === "confirmed" ? "#22c55e" : "#94a3b8" }}
                    />
                    <span className="text-muted-foreground">
                      {e.relationType || "—"} · {e.status === "confirmed" ? "real" : "pista"}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-medium">{other?.label ?? otherKey}</div>
                </li>
              )
            })}
          </ul>
        )}
      </div>
      {node.sourceInboxIds.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">
            Correos de origen ({node.sourceInboxIds.length})
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {node.sourceInboxIds.map((iid) => (
              <li key={iid}>
                <Link
                  to={`/datos/${iid}`}
                  className="inline-block rounded border bg-muted/30 px-2 py-0.5 text-xs text-origin-inbox hover:underline"
                >
                  correo #{iid}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
      {Object.entries(KIND_LABEL).map(([k, label]) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2.5 rounded-full" style={{ background: kindColor(k) }} />
          {label}
        </span>
      ))}
      <span className="ml-1 inline-flex items-center gap-1.5">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke="#22c55e" strokeWidth="2" />
        </svg>
        Real
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke="#94a3b8" strokeWidth="1.5" strokeDasharray="4 3" />
        </svg>
        Pista
      </span>
    </div>
  )
}

export function GraphPage() {
  // `?inbox_id=` enfoca el grafo en lo que produjo ese correo (botón "ver en grafo" desde /datos/:id).
  const [searchParams] = useSearchParams()
  const inboxParam = searchParams.get("inbox_id")
  const inboxId = inboxParam ? Number(inboxParam) : undefined
  const [status, setStatus] = useState<string>("todas")
  const [onlyConnected, setOnlyConnected] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [building, setBuilding] = useState(false)
  const { data, loading, error, reload } = useAsync<GraphData>(
    () => fetchGraph(status === "todas" ? undefined : status, inboxId),
    [status, inboxId],
  )
  const full = data ?? { nodes: [], edges: [] }

  // Filtro "solo conectados": esconde los vértices sin ninguna arista (ruido para una vista de relaciones).
  const shown = useMemo<GraphData>(() => {
    if (!onlyConnected) return full
    const connected = new Set<string>()
    for (const e of full.edges) {
      connected.add(nodeKey(e.srcSlug, e.srcId))
      connected.add(nodeKey(e.dstSlug, e.dstId))
    }
    return { nodes: full.nodes.filter((n) => connected.has(nodeKey(n.slug, n.id))), edges: full.edges }
  }, [full, onlyConnected])

  const nodesByKey = useMemo(
    () => new Map(full.nodes.map((n) => [nodeKey(n.slug, n.id), n])),
    [full.nodes],
  )
  const selectedNode = selected ? nodesByKey.get(selected) ?? null : null
  const hiddenCount = full.nodes.length - shown.nodes.length

  async function onBuild() {
    setBuilding(true)
    try {
      const r = await buildGraph()
      const reales = r.afiliacionReales + r.pertenenciaReales + r.contraparteReales
      toast.success(
        `Grafo armado: ${r.cooccurrencePistas} pistas, ${reales} reales (${r.contraparteReales} contraparte)` +
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
        description="Cada vértice es una entidad única (cobro/pago, evento, hackatón, persona, organización). Las aristas las forman el inbox (co-ocurrencia = PISTA, sin vouchar) y los datos reales (afiliación/pertenencia del directorio y la contraparte de cada cobro→identidad = REAL); el LLM valida las pistas después. Filtrá «Reales» para ver solo lo confirmado. Rueda = zoom · arrastrá = mover · click en un nodo = ver sus relaciones."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {inboxId != null && (
              <Link
                to="/grafo"
                className="num inline-flex items-center gap-1 rounded-full border border-brand/40 bg-brand/10 px-2 py-0.5 text-[11px] text-brand hover:bg-brand/20"
                title="Quitar el filtro y ver el grafo completo"
              >
                correo #{inboxId} · quitar filtro ✕
              </Link>
            )}
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch checked={onlyConnected} onCheckedChange={setOnlyConnected} aria-label="Solo conectados" />
              Solo conectados
            </label>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="h-8 w-auto min-w-[90px] text-xs" aria-label="Nivel de arista">
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
      ) : full.nodes.length === 0 ? (
        <EmptyState
          title="Grafo vacío"
          hint="Todavía no hay vértices. Corré la extracción en Procesamiento; cuando haya datos, tocá «Armar grafo»."
        />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-xs text-muted-foreground">
              {shown.nodes.length} vértices · {shown.edges.length} aristas
              {hiddenCount > 0 && ` · ${hiddenCount} aislados ocultos`}
            </div>
            <Legend />
          </div>
          {onlyConnected && shown.edges.length === 0 ? (
            <EmptyState
              title="Sin relaciones todavía"
              hint="No hay aristas deterministas. Tocá «Armar grafo», o apagá «Solo conectados» para ver los vértices sueltos."
            />
          ) : (
            <div className="flex flex-col gap-3 md:flex-row">
              <div className="relative min-w-0 flex-1 overflow-hidden rounded-lg border bg-muted/20">
                <GraphCanvas data={shown} selected={selected} onSelect={setSelected} />
                <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-1 text-[11px] text-muted-foreground">
                  <Maximize2 className="size-3" /> rueda = zoom · arrastrá = mover
                </div>
              </div>
              {selectedNode && (
                <DetailPanel node={selectedNode} edges={full.edges} nodesByKey={nodesByKey} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
