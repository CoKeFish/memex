import { useCallback, useMemo, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { Boxes, CheckCheck, Clock, Hammer, Loader2, Maximize2, Minimize2, Sparkles } from "lucide-react"
import { toast } from "sonner"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { clusterGraph, confirmCooccurrences, fetchGraph, reconcileGraph, validateClusters } from "@/data"
import type { GraphData, GraphEdge, GraphNode } from "@/data/graph"
// El plegado por cúmulo (miembros ocultos + aristas re-ruteadas al nodo cúmulo) es una función pura.
import { collapseClusters, type CollapsedEdge } from "@/lib/graph-collapse"
import { CUMULO_COLOR, KIND_LABEL, kindColor } from "@/lib/graph-kind"
// El layout (d3-force por componente conexa + shelf packing) vive en graph-layout: funciones puras.
import { baseRadius, layoutGraph, nodeKey, type Bounds } from "@/lib/graph-layout"
import { inboxRefLabel } from "@/lib/inbox-format"
import { useAsync } from "@/lib/use-async"

const VERDICT_OPTIONS: { value: string; label: string }[] = [
  { value: "todas", label: "Todas" },
  { value: "confirmed", label: "Confirmadas" },
  { value: "ambiguous", label: "Ambiguas" },
]

// Colores por etiqueta canónica (dos ejes): EXTRACTED verde fuerte (hecho determinista), INFERRED
// verde claro (el LLM lo dedujo), INFERRED REJECTED rojo punteado, AMBIGUOUS gris punteado.
function edgeColor(verdict: string, provenance: string): string {
  if (verdict === "confirmed") return provenance === "extracted" ? "#16a34a" : "#22c55e"
  if (verdict === "rejected") return "#ef4444"
  return "#94a3b8"
}

const EMPTY_GRAPH: GraphData = { nodes: [], edges: [], inboxKinds: {} }

// Viewport interno del SVG (coordenadas fijas; el CSS lo escala responsivo).
const VW = 1000
const VH = 600
const FIT_PAD = 50

interface View {
  x: number
  y: number
  k: number
}

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v))

function fitView(b: Bounds): View {
  const w = b.maxX - b.minX || 1
  const h = b.maxY - b.minY || 1
  // el piso del zoom es bajo a propósito: el mosaico de componentes debe entrar ENTERO al encuadrar
  const k = clamp(Math.min((VW - 2 * FIT_PAD) / w, (VH - 2 * FIT_PAD) / h), 0.05, 3)
  const cx = (b.minX + b.maxX) / 2
  const cy = (b.minY + b.maxY) / 2
  return { k, x: VW / 2 - cx * k, y: VH / 2 - cy * k }
}

function edgeStyle(e: CollapsedEdge): { stroke: string; width: number; dash?: string } {
  if (e.relationType === "agregada") {
    // sintética del plegado: agrega N aristas re-ruteadas al cúmulo → trazo escalado por N
    const w = 1.5 + Math.min(e.aggregateCount ?? 1, 6) * 0.35
    return { stroke: edgeColor(e.verdict, e.provenance), width: w }
  }
  if (e.relationType === "miembro_de") return { stroke: CUMULO_COLOR, width: 2.2 } // membresía de cúmulo
  const stroke = edgeColor(e.verdict, e.provenance)
  if (e.verdict === "confirmed") return { stroke, width: e.provenance === "extracted" ? 2.2 : 2 }
  if (e.verdict === "rejected") return { stroke, width: 1.5, dash: "2 4" }
  return { stroke, width: 1.5, dash: "5 4" } // ambiguous
}

function edgeTitle(e: CollapsedEdge): string {
  if (e.relationType === "agregada") return `${e.aggregateCount ?? 1} relaciones (plegadas) · ${e.label}`
  const just = e.relation ? ` · ${e.relation}` : ""
  return `${e.relationType || "—"} · ${e.label} · ${e.producer}${just}`
}

function GraphCanvas({
  data,
  selected,
  selectedEdgeId,
  onSelect,
  onSelectEdge,
}: {
  data: GraphData
  selected: string | null
  selectedEdgeId: number | null
  onSelect: (k: string | null) => void
  onSelectEdge: (id: number | null) => void
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<string | null>(null)
  const lay = useMemo(() => layoutGraph(data.nodes, data.edges), [data.nodes, data.edges])
  const [view, setView] = useState<View>(() => fitView(lay.bounds))
  // Re-encuadra al cambiar el conjunto (filtro/armado), no en cada render. Ajuste de estado
  // DURANTE el render (patrón "derivar al cambiar la prop") en vez de un effect: evita el
  // frame intermedio con el encuadre viejo y el setState-in-effect.
  const [prevLay, setPrevLay] = useState(lay)
  if (prevLay !== lay) {
    setPrevLay(lay)
    setView(fitView(lay.bounds))
  }

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
    // click en el fondo (sin arrastre) = deseleccionar (nodo y arista)
    if (drag.current && !drag.current.moved) {
      onSelect(null)
      onSelectEdge(null)
    }
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
          const st = edgeStyle(e)
          const lit = !focus || (incident.has(a.key) && incident.has(b.key))
          const isSelEdge = selectedEdgeId === e.id
          // Las aristas sintéticas del plegado (id negativo) no son seleccionables (no son reales).
          const selectable = e.id > 0
          return (
            <g key={e.id} opacity={lit ? 0.85 : 0.07}>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={st.stroke}
                strokeWidth={(isSelEdge ? st.width + 2 : st.width) / view.k}
                strokeDasharray={
                  st.dash ? st.dash.split(" ").map((n) => Number(n) / view.k).join(" ") : undefined
                }
              >
                <title>{edgeTitle(e)}</title>
              </line>
              {selectable && (
                // Banda invisible más ancha: hace clickeable la arista delgada sin tapar nada.
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke="transparent"
                  strokeWidth={10 / view.k}
                  style={{ cursor: "pointer" }}
                  onClick={(ev) => {
                    ev.stopPropagation()
                    onSelect(null)
                    onSelectEdge(isSelEdge ? null : e.id)
                  }}
                />
              )}
            </g>
          )
        })}
        {lay.sims.map((s) => {
          const isCumulo = s.node.slug === "cumulo"
          const r = baseRadius(s.node, s.deg) / view.k
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
              {isCumulo && (
                <circle
                  r={r + 5 / view.k}
                  fill={CUMULO_COLOR}
                  opacity={0.14}
                  stroke={CUMULO_COLOR}
                  strokeOpacity={0.5}
                  strokeWidth={1.2 / view.k}
                />
              )}
              <circle
                r={r}
                fill={kindColor(s.node.kind)}
                stroke={isSel ? "#0f172a" : "white"}
                strokeWidth={(isSel ? 2.5 : 1.5) / view.k}
              />
              {(labelsAlways || isCumulo || focus === s.key) && (
                <text
                  y={r + 11 / view.k}
                  textAnchor="middle"
                  fontSize={(isCumulo ? 11 : 10) / view.k}
                  className="fill-foreground"
                  style={{ pointerEvents: "none", fontWeight: isCumulo ? 600 : 400 }}
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

function DetailPanel({
  node,
  edges,
  nodesByKey,
  inboxKinds,
  expandedClusters,
  showFull,
  onToggleCluster,
}: {
  node: GraphNode
  edges: GraphEdge[]
  nodesByKey: Map<string, GraphNode>
  inboxKinds: Record<number, string>
  expandedClusters: ReadonlySet<number>
  showFull: boolean
  onToggleCluster: (id: number) => void
}) {
  const mine = edges.filter(
    (e) => nodeKey(e.srcSlug, e.srcId) === nodeKey(node.slug, node.id) || nodeKey(e.dstSlug, e.dstId) === nodeKey(node.slug, node.id),
  )
  const isCumulo = node.slug === "cumulo"
  const memberCount = isCumulo
    ? edges.filter((e) => e.relationType === "miembro_de" && e.dstSlug === "cumulo" && e.dstId === node.id).length
    : 0
  const isExpanded = expandedClusters.has(node.id)
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
      {isCumulo && (
        <Link
          to={`/grafo/cumulo/${node.id}`}
          className="flex items-center justify-center gap-1.5 rounded-md border bg-muted/30 px-2 py-1.5 text-xs font-medium hover:bg-muted/60"
        >
          <Clock className="size-3.5" /> Abrir cronología
        </Link>
      )}
      {isCumulo && !showFull && memberCount > 0 && (
        <button
          type="button"
          onClick={() => onToggleCluster(node.id)}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border bg-muted/30 px-2 py-1.5 text-xs font-medium hover:bg-muted/60"
        >
          {isExpanded ? (
            <>
              <Minimize2 className="size-3.5" /> Plegar miembros
            </>
          ) : (
            <>
              <Maximize2 className="size-3.5" /> Expandir miembros ({memberCount})
            </>
          )}
        </button>
      )}
      <div>
        <div className="mb-1 text-xs font-medium text-muted-foreground">
          {isCumulo ? `Miembros (${mine.length})` : `Relaciones (${mine.length})`}
        </div>
        {mine.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {isCumulo ? "Sin miembros." : "Sin aristas. (Las semánticas llegan con el LLM.)"}
          </p>
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
                      style={{ background: edgeColor(e.verdict, e.provenance) }}
                    />
                    <span className="text-muted-foreground">
                      {e.relationType || "—"} · {e.label}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate font-medium">{other?.label ?? otherKey}</div>
                  {e.relation && <div className="mt-0.5 text-[11px] italic text-muted-foreground">{e.relation}</div>}
                  {/* procedencia de la ARISTA: TODOS los mensajes que la generaron (no solo el
                      primero del evidence) — mismo drill-down que «Mensajes de origen» del nodo */}
                  {e.sourceInboxIds.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {e.sourceInboxIds.slice(0, 6).map((iid) => (
                        <Link
                          key={iid}
                          to={`/datos/${iid}`}
                          className="rounded border bg-muted/30 px-1.5 py-0.5 text-[11px] text-origin-inbox hover:underline"
                        >
                          {inboxRefLabel(iid, inboxKinds)}
                        </Link>
                      ))}
                      {e.sourceInboxIds.length > 6 && (
                        <span className="px-1 py-0.5 text-[11px] text-muted-foreground">
                          +{e.sourceInboxIds.length - 6} más
                        </span>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
      {node.sourceInboxIds.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">
            Mensajes de origen ({node.sourceInboxIds.length})
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {/* tope: un canal/remitente activo arrastra MILES de mensajes; el panel no es la lista */}
            {node.sourceInboxIds.slice(0, 40).map((iid) => (
              <li key={iid}>
                <Link
                  to={`/datos/${iid}`}
                  className="inline-block rounded border bg-muted/30 px-2 py-0.5 text-xs text-origin-inbox hover:underline"
                >
                  {inboxRefLabel(iid, inboxKinds)}
                </Link>
              </li>
            ))}
            {node.sourceInboxIds.length > 40 && (
              <li className="px-1 py-0.5 text-xs text-muted-foreground">
                +{node.sourceInboxIds.length - 40} más
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Panel de una ARISTA seleccionada: su etiqueta canónica, la justificación (`relation`), el tipo,
 * la confianza y los mensajes de origen (drill-down). El producto del grafo es la relación + su
 * procedencia, no el dibujo. */
function EdgeDetailPanel({
  edge,
  nodesByKey,
  inboxKinds,
}: {
  edge: GraphEdge
  nodesByKey: Map<string, GraphNode>
  inboxKinds: Record<number, string>
}) {
  const src = nodesByKey.get(nodeKey(edge.srcSlug, edge.srcId))
  const dst = nodesByKey.get(nodeKey(edge.dstSlug, edge.dstId))
  const allChat =
    edge.sourceInboxIds.length > 0 && edge.sourceInboxIds.every((iid) => inboxKinds[iid] === "chat")
  return (
    <div className="w-full shrink-0 space-y-3 rounded-lg border bg-card p-3 text-sm md:w-72">
      <div>
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-1 w-5 rounded"
            style={{ background: edgeColor(edge.verdict, edge.provenance) }}
          />
          <span className="text-xs font-semibold uppercase tracking-wide">{edge.label}</span>
          {allChat && (
            <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">chat</span>
          )}
        </div>
        <div className="mt-1.5 font-medium leading-tight">
          {src?.label ?? `${edge.srcSlug}#${edge.srcId}`}
          <span className="mx-1 text-muted-foreground">↔</span>
          {dst?.label ?? `${edge.dstSlug}#${edge.dstId}`}
        </div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          {edge.relationType || "—"} · {edge.producer}
          {edge.confidence != null && ` · ${(edge.confidence * 100).toFixed(0)}%`}
        </div>
      </div>
      {edge.relation && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">Por qué existe</div>
          <p className="rounded border bg-muted/30 px-2 py-1.5 text-xs italic">{edge.relation}</p>
        </div>
      )}
      {edge.sourceInboxIds.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">
            Mensajes de origen ({edge.sourceInboxIds.length})
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {edge.sourceInboxIds.slice(0, 20).map((iid) => (
              <li key={iid}>
                <Link
                  to={`/datos/${iid}`}
                  className="inline-block rounded border bg-muted/30 px-2 py-0.5 text-xs text-origin-inbox hover:underline"
                >
                  {inboxRefLabel(iid, inboxKinds)}
                </Link>
              </li>
            ))}
            {edge.sourceInboxIds.length > 20 && (
              <li className="px-1 py-0.5 text-xs text-muted-foreground">
                +{edge.sourceInboxIds.length - 20} más
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Leyenda-FILTRO: cada tipo presente en el grafo es un toggle (click = ocultar/mostrar sus
 * vértices); las entradas de aristas (EXTRACTED/INFERRED/AMBIGUOUS/Miembro) son informativas. */
function Legend({
  kinds,
  hidden,
  onToggle,
}: {
  kinds: { kind: string; count: number }[]
  hidden: ReadonlySet<string>
  onToggle: (k: string) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs text-muted-foreground">
      {kinds.map(({ kind, count }) => {
        const off = hidden.has(kind)
        return (
          <button
            key={kind}
            type="button"
            aria-pressed={!off}
            onClick={() => onToggle(kind)}
            title={off ? "Mostrar este tipo" : "Ocultar este tipo"}
            className={`inline-flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-muted/60 ${
              off ? "line-through opacity-40" : ""
            }`}
          >
            <span className="inline-block size-2.5 rounded-full" style={{ background: kindColor(kind) }} />
            {KIND_LABEL[kind] ?? kind} <span className="num">({count})</span>
          </button>
        )
      })}
      <span className="ml-1 inline-flex items-center gap-1.5" title="Hecho leído de la fuente">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke="#16a34a" strokeWidth="2.2" />
        </svg>
        EXTRACTED
      </span>
      <span className="inline-flex items-center gap-1.5" title="El LLM lo dedujo">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke="#22c55e" strokeWidth="2" />
        </svg>
        INFERRED
      </span>
      <span className="inline-flex items-center gap-1.5" title="Sospecha sin decidir">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke="#94a3b8" strokeWidth="1.5" strokeDasharray="5 4" />
        </svg>
        AMBIGUOUS
      </span>
      <span className="inline-flex items-center gap-1.5">
        <svg width="20" height="6">
          <line x1="0" y1="3" x2="20" y2="3" stroke={CUMULO_COLOR} strokeWidth="2.2" />
        </svg>
        Miembro de cúmulo
      </span>
    </div>
  )
}

export function GraphPage() {
  // `?inbox_id=` enfoca el grafo en lo que produjo ese mensaje (botón "ver en grafo" desde /datos/:id).
  const [searchParams] = useSearchParams()
  const inboxParam = searchParams.get("inbox_id")
  const inboxId = inboxParam ? Number(inboxParam) : undefined
  const [verdict, setVerdict] = useState<string>("todas")
  const [onlyConnected, setOnlyConnected] = useState(true)
  // Reglas para chats: las co-ocurrencias ambiguas de mensajes de chat son ruidosas (la fase de
  // confirmación ni las juzga por default); ocultarlas es el default.
  const [hideChatAmbiguous, setHideChatAmbiguous] = useState(true)
  const [hiddenKinds, setHiddenKinds] = useState<ReadonlySet<string>>(() => new Set())
  // Plegado por cúmulo: default TODO plegado (los miembros se ocultan y sus aristas se re-rutean
  // al nodo cúmulo); `expanded` guarda los cúmulos abiertos y «Ver completo» lo apaga entero.
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(() => new Set())
  const [showFull, setShowFull] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<number | null>(null)
  const [reconciling, setReconciling] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [clustering, setClustering] = useState(false)
  const [validating, setValidating] = useState(false)
  const { data, loading, error, reload } = useAsync<GraphData>(
    () => fetchGraph(verdict === "todas" ? undefined : verdict, inboxId),
    [verdict, inboxId],
  )
  const full = data ?? EMPTY_GRAPH

  // Filtros del front en 4 etapas: (1) tipos ocultos por la leyenda, (2) aristas con algún extremo
  // oculto, (3) PLEGADO por cúmulo (salvo «Ver completo»), (4) "solo conectados" sobre lo que
  // queda (esconde aislados; los cúmulos se eximen: plegados, sus aristas son internas y un
  // contexto confirmado merece verse aunque no toque nada externo).
  const shown = useMemo<GraphData>(() => {
    const visible = hiddenKinds.size
      ? full.nodes.filter((n) => !hiddenKinds.has(n.kind))
      : full.nodes
    const present = new Set(visible.map((n) => nodeKey(n.slug, n.id)))
    // Chat-ambigua: co-ocurrencia sin juzgar cuya evidencia es TODA de chats (ruido por default).
    const isChatAmbiguous = (e: GraphEdge): boolean =>
      e.verdict === "ambiguous" &&
      e.sourceInboxIds.length > 0 &&
      e.sourceInboxIds.every((iid) => full.inboxKinds[iid] === "chat")
    const baseEdges = hideChatAmbiguous ? full.edges.filter((e) => !isChatAmbiguous(e)) : full.edges
    const visibleEdges = baseEdges.filter(
      (e) => present.has(nodeKey(e.srcSlug, e.srcId)) && present.has(nodeKey(e.dstSlug, e.dstId)),
    )
    const { nodes, edges } = showFull
      ? { nodes: visible, edges: visibleEdges }
      : collapseClusters(visible, visibleEdges, expanded)
    if (!onlyConnected) return { nodes, edges, inboxKinds: full.inboxKinds }
    const connected = new Set<string>()
    for (const e of edges) {
      connected.add(nodeKey(e.srcSlug, e.srcId))
      connected.add(nodeKey(e.dstSlug, e.dstId))
    }
    return {
      nodes: nodes.filter((n) => n.slug === "cumulo" || connected.has(nodeKey(n.slug, n.id))),
      edges,
      inboxKinds: full.inboxKinds,
    }
  }, [full, onlyConnected, hiddenKinds, showFull, expanded, hideChatAmbiguous])

  const nodesByKey = useMemo(
    () => new Map(full.nodes.map((n) => [nodeKey(n.slug, n.id), n])),
    [full.nodes],
  )
  // Si el nodo elegido queda oculto (plegado/filtro), soltarlo — mismo patrón "derivar al cambiar"
  // que el re-encuadre del canvas (sin effect ni frame intermedio).
  const shownKeys = useMemo(
    () => new Set(shown.nodes.map((n) => nodeKey(n.slug, n.id))),
    [shown.nodes],
  )
  // La arista seleccionada se busca en el set MOSTRADO (si un filtro la sacó, se suelta).
  const shownEdgeIds = useMemo(() => new Set(shown.edges.map((e) => e.id)), [shown.edges])
  const [prevShownKeys, setPrevShownKeys] = useState(shownKeys)
  if (prevShownKeys !== shownKeys) {
    setPrevShownKeys(shownKeys)
    if (selected && !shownKeys.has(selected)) setSelected(null)
  }
  const [prevShownEdgeIds, setPrevShownEdgeIds] = useState(shownEdgeIds)
  if (prevShownEdgeIds !== shownEdgeIds) {
    setPrevShownEdgeIds(shownEdgeIds)
    if (selectedEdgeId != null && !shownEdgeIds.has(selectedEdgeId)) setSelectedEdgeId(null)
  }
  const selectedNode = selected ? nodesByKey.get(selected) ?? null : null
  const selectedEdge =
    selectedEdgeId != null ? full.edges.find((e) => e.id === selectedEdgeId) ?? null : null
  // Seleccionar un nodo limpia la arista (paneles mutuamente excluyentes).
  const selectNode = (k: string | null) => {
    setSelected(k)
    if (k) setSelectedEdgeId(null)
  }
  const hiddenCount = full.nodes.length - shown.nodes.length

  function toggleCluster(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Tipos presentes en el grafo (orden de la leyenda; los desconocidos al final) con su conteo.
  const legendKinds = useMemo(() => {
    const counts = new Map<string, number>()
    for (const n of full.nodes) counts.set(n.kind, (counts.get(n.kind) ?? 0) + 1)
    const known = Object.keys(KIND_LABEL).filter((k) => counts.has(k))
    const unknown = [...counts.keys()].filter((k) => !(k in KIND_LABEL)).sort()
    return [...known, ...unknown].map((k) => ({ kind: k, count: counts.get(k) ?? 0 }))
  }, [full.nodes])

  function toggleKind(k: string) {
    // si el nodo elegido queda oculto, soltarlo (el panel mostraría algo invisible)
    if (!hiddenKinds.has(k) && selectedNode?.kind === k) setSelected(null)
    setHiddenKinds((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }

  async function onReconcile() {
    setReconciling(true)
    try {
      const r = await reconcileGraph()
      const stale = r.staleAfiliacion + r.stalePertenencia + r.staleContraparte
      toast.success(
        `Mantenimiento: ${stale} reales reconciliadas · ${r.orphansPruned} huérfanas podadas`,
      )
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "No se pudo reconciliar el grafo")
    } finally {
      setReconciling(false)
    }
  }

  async function onConfirm() {
    if (!window.confirm("¿Confirmar las co-ocurrencias ambiguas por-mensaje con el LLM? Tiene un costo por llamada.")) return
    setConfirming(true)
    try {
      const r = await confirmCooccurrences()
      toast.success(
        `Confirmación: ${r.llmConfirmed} por LLM · ${r.confirmedRecibo} por recibo · ` +
          `${r.gated} bloqueadas · ${r.summaries} resúmenes ($${r.costUsd.toFixed(4)})`,
      )
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "No se pudo confirmar (¿DEEPSEEK_API_KEY?)")
    } finally {
      setConfirming(false)
    }
  }

  async function onCluster() {
    setClustering(true)
    try {
      const r = await clusterGraph()
      toast.success(
        `Cúmulos: ${r.detected} detectados · ${r.newCandidates} nuevos · ${r.dissolved} disueltos`,
      )
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "No se pudieron detectar los cúmulos")
    } finally {
      setClustering(false)
    }
  }

  async function onValidate() {
    if (!window.confirm("¿Validar los cúmulos pendientes con el LLM? Tiene un costo por llamada.")) return
    setValidating(true)
    try {
      const r = await validateClusters()
      toast.success(
        `Particionado: ${r.blobs} blobs → ${r.groups} contextos · ` +
          `${r.promoted} pistas promovidas ($${r.costUsd.toFixed(4)})`,
      )
      reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "No se pudo validar (¿DEEPSEEK_API_KEY?)")
    } finally {
      setValidating(false)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="relaciones · grafo"
        title="Grafo de relaciones"
        description="Cada vértice es una entidad única (cobro/pago, evento, hackatón, persona, organización). Cada arista lleva su PROCEDENCIA × VEREDICTO: EXTRACTED (hecho leído de la fuente), INFERRED (el LLM lo dedujo) y AMBIGUOUS (co-ocurrencia sin juzgar). «Confirmar (LLM)» abre cada mensaje y juzga sus co-ocurrencias ambiguas (verde = confirmada, con su justificación). Los CÚMULOS (nodos violeta) son grupos que el LLM validó como un contexto. Rueda = zoom · arrastrá = mover · click en un nodo o una arista = ver el detalle."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {inboxId != null && (
              <Link
                to="/grafo"
                className="num inline-flex items-center gap-1 rounded-full border border-brand/40 bg-brand/10 px-2 py-0.5 text-[11px] text-brand hover:bg-brand/20"
                title="Quitar el filtro y ver el grafo completo"
              >
                {inboxRefLabel(inboxId, full.inboxKinds)} · quitar filtro ✕
              </Link>
            )}
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch checked={showFull} onCheckedChange={setShowFull} aria-label="Ver completo" />
              Ver completo
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch checked={onlyConnected} onCheckedChange={setOnlyConnected} aria-label="Solo conectados" />
              Solo conectados
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Switch
                checked={hideChatAmbiguous}
                onCheckedChange={setHideChatAmbiguous}
                aria-label="Ocultar chats ambiguos"
              />
              Ocultar chats ambiguos
            </label>
            <Select value={verdict} onValueChange={setVerdict}>
              <SelectTrigger className="h-8 w-auto min-w-[90px] text-xs" aria-label="Veredicto de arista">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {VERDICT_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value} className="text-xs">
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button size="sm" variant="outline" onClick={onReconcile} disabled={reconciling}>
              {reconciling ? <Loader2 className="size-4 animate-spin" /> : <Hammer className="size-4" />}
              Reconciliar
            </Button>
            <Button size="sm" variant="outline" onClick={onConfirm} disabled={confirming}>
              {confirming ? <Loader2 className="size-4 animate-spin" /> : <CheckCheck className="size-4" />}
              Confirmar (LLM)
            </Button>
            <Button size="sm" variant="outline" onClick={onCluster} disabled={clustering}>
              {clustering ? <Loader2 className="size-4 animate-spin" /> : <Boxes className="size-4" />}
              Detectar cúmulos
            </Button>
            <Button size="sm" variant="outline" onClick={onValidate} disabled={validating}>
              {validating ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
              Validar (LLM)
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
          hint="Todavía no hay vértices. Corré la extracción en Procesamiento: los vértices y sus aristas reales se tejen al procesar."
        />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-xs text-muted-foreground">
              {shown.nodes.length} vértices · {shown.edges.length} aristas
              {hiddenCount > 0 && ` · ${hiddenCount} ocultos`}
            </div>
            <Legend kinds={legendKinds} hidden={hiddenKinds} onToggle={toggleKind} />
          </div>
          {shown.nodes.length === 0 || (onlyConnected && shown.edges.length === 0) ? (
            <EmptyState
              title={hiddenKinds.size > 0 ? "Sin vértices visibles" : "Sin relaciones todavía"}
              hint={
                hiddenKinds.size > 0
                  ? "Todos los vértices quedaron filtrados: reactivá tipos en la leyenda o apagá «Solo conectados»."
                  : "No hay aristas. Las reales se tejen al procesar; las pistas, con «Confirmar». O apagá «Solo conectados» para ver los vértices sueltos."
              }
            />
          ) : (
            <div className="flex flex-col gap-3 md:flex-row">
              <div className="relative min-w-0 flex-1 overflow-hidden rounded-lg border bg-muted/20">
                <GraphCanvas
                  data={shown}
                  selected={selected}
                  selectedEdgeId={selectedEdgeId}
                  onSelect={selectNode}
                  onSelectEdge={setSelectedEdgeId}
                />
                <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-1 text-[11px] text-muted-foreground">
                  <Maximize2 className="size-3" /> rueda = zoom · arrastrá = mover
                </div>
              </div>
              {selectedEdge ? (
                <EdgeDetailPanel
                  edge={selectedEdge}
                  nodesByKey={nodesByKey}
                  inboxKinds={full.inboxKinds}
                />
              ) : (
                selectedNode && (
                  <DetailPanel
                    node={selectedNode}
                    edges={full.edges}
                    nodesByKey={nodesByKey}
                    inboxKinds={full.inboxKinds}
                    expandedClusters={expanded}
                    showFull={showFull}
                    onToggleCluster={toggleCluster}
                  />
                )
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
