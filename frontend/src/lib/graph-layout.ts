// Layout del grafo de relaciones: cada componente conexa se simula POR SEPARADO con d3-force
// (sincrónico y determinista) y las cajas resultantes se EMPAQUETAN en filas (shelf packing).
// Separar simulación de empaquetado evita el problema clásico del force-directed con grafos
// desconectados: un solo centro atrae todas las componentes al mismo punto y terminan montadas
// unas sobre otras. Funciones puras, sin React.

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force"
import type { GraphEdge, GraphNode } from "@/data/graph"

export const nodeKey = (slug: string, id: number): string => `${slug}#${id}`

export interface PlacedNode {
  key: string
  node: GraphNode
  x: number
  y: number
  deg: number
}

export interface Bounds {
  minX: number
  minY: number
  maxX: number
  maxY: number
}

export interface GraphLayout {
  sims: PlacedNode[]
  byKey: Map<string, PlacedNode>
  bounds: Bounds
}

/** Radio base del nodo en coordenadas de mundo (el render lo divide por el zoom). */
export function baseRadius(node: GraphNode, deg: number): number {
  return node.slug === "cumulo" ? 11 + Math.min(deg, 16) * 0.7 : 6 + Math.min(deg, 10) * 0.6
}

export interface Component {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

/** Componentes conexas (BFS, orden estable por aparición). Las aristas con un extremo ausente del
 * set de nodos se ignoran (el server las poda, pero los filtros del front pueden recortarlas). */
export function connectedComponents(nodes: GraphNode[], edges: GraphEdge[]): Component[] {
  const keys = new Set(nodes.map((n) => nodeKey(n.slug, n.id)))
  const adj = new Map<string, string[]>()
  const push = (k: string, v: string) => {
    const arr = adj.get(k)
    if (arr) arr.push(v)
    else adj.set(k, [v])
  }
  const valid: GraphEdge[] = []
  for (const e of edges) {
    const a = nodeKey(e.srcSlug, e.srcId)
    const b = nodeKey(e.dstSlug, e.dstId)
    if (!keys.has(a) || !keys.has(b)) continue
    valid.push(e)
    push(a, b)
    push(b, a)
  }

  const compOf = new Map<string, number>()
  let count = 0
  for (const n of nodes) {
    const start = nodeKey(n.slug, n.id)
    if (compOf.has(start)) continue
    compOf.set(start, count)
    const queue = [start]
    for (let qi = 0; qi < queue.length; qi++) {
      for (const nb of adj.get(queue[qi]) ?? []) {
        if (!compOf.has(nb)) {
          compOf.set(nb, count)
          queue.push(nb)
        }
      }
    }
    count++
  }

  const comps: Component[] = Array.from({ length: count }, () => ({ nodes: [], edges: [] }))
  for (const n of nodes) {
    const c = compOf.get(nodeKey(n.slug, n.id))
    if (c !== undefined) comps[c].nodes.push(n)
  }
  for (const e of valid) {
    const c = compOf.get(nodeKey(e.srcSlug, e.srcId))
    if (c !== undefined) comps[c].edges.push(e)
  }
  return comps
}

export interface Box {
  w: number
  h: number
}

/** Empaquetado en filas (shelf): techo de ancho ≈ sqrt(área_total · aspect) para apuntar al
 * aspecto pedido; las cajas se colocan EN EL ORDEN DE ENTRADA (el caller ordena por alto desc —
 * FFDH — para minimizar el desperdicio). Devuelve el offset top-left de cada caja, índice a
 * índice. */
export function shelfPack(boxes: Box[], gap: number, targetAspect: number): { x: number; y: number }[] {
  let area = 0
  let maxW = 0
  for (const b of boxes) {
    area += (b.w + gap) * (b.h + gap)
    maxW = Math.max(maxW, b.w)
  }
  const ceiling = Math.max(Math.sqrt(area * targetAspect), maxW)
  const out: { x: number; y: number }[] = []
  let x = 0
  let y = 0
  let rowH = 0
  for (const b of boxes) {
    if (x > 0 && x + b.w > ceiling) {
      x = 0
      y += rowH + gap
      rowH = 0
    }
    out.push({ x, y })
    x += b.w + gap
    rowH = Math.max(rowH, b.h)
  }
  return out
}

interface SimNode extends SimulationNodeDatum {
  key: string
  node: GraphNode
  deg: number
  r: number
}

// Margen alrededor del bbox de cada componente y espacio entre cajas al empaquetar.
const PAD = 14
const GAP = 18
// Aspecto objetivo del mosaico = el del viewport del SVG en graph.tsx (1000×600).
const TARGET_ASPECT = 1000 / 600

/** Layout completo y DETERMINISTA: grados → componentes → d3-force por componente (sincrónico,
 * `randomSource` default de d3-force = LCG sembrado; nada usa Math.random) → shelf packing.
 * Los nodos aislados no se simulan (caja fija): quedan como una alfombra ordenada al final. */
export function layoutGraph(nodes: GraphNode[], edges: GraphEdge[]): GraphLayout {
  if (!nodes.length) {
    return { sims: [], byKey: new Map(), bounds: { minX: -100, minY: -100, maxX: 100, maxY: 100 } }
  }

  const keys = new Set(nodes.map((n) => nodeKey(n.slug, n.id)))
  const deg = new Map<string, number>()
  for (const e of edges) {
    const a = nodeKey(e.srcSlug, e.srcId)
    const b = nodeKey(e.dstSlug, e.dstId)
    if (!keys.has(a) || !keys.has(b)) continue
    deg.set(a, (deg.get(a) ?? 0) + 1)
    deg.set(b, (deg.get(b) ?? 0) + 1)
  }

  // 1) posiciones LOCALES por componente (alrededor de su propio origen) + su caja.
  const comps = connectedComponents(nodes, edges)
  const placed: PlacedNode[][] = []
  const boxes: Box[] = []
  const mins: { x: number; y: number }[] = []
  for (const comp of comps) {
    const simNodes: SimNode[] = comp.nodes.map((node) => {
      const key = nodeKey(node.slug, node.id)
      const d = deg.get(key) ?? 0
      return { key, node, deg: d, r: baseRadius(node, d) }
    })
    if (simNodes.length > 1) {
      const links: SimulationLinkDatum<SimNode>[] = comp.edges.map((e) => ({
        source: nodeKey(e.srcSlug, e.srcId),
        target: nodeKey(e.dstSlug, e.dstId),
      }))
      const sim = forceSimulation(simNodes)
        .force(
          "link",
          forceLink<SimNode, SimulationLinkDatum<SimNode>>(links)
            .id((d) => d.key)
            .distance(70),
        )
        .force("charge", forceManyBody<SimNode>().strength(-220).distanceMax(380))
        .force("x", forceX<SimNode>(0).strength(0.08))
        // un pelo más fuerte en Y → cajas más anchas que altas (acompaña el viewport apaisado)
        .force("y", forceY<SimNode>(0).strength(0.1))
        .force(
          "collide",
          forceCollide<SimNode>((d) => d.r + 4).iterations(2),
        )
        .stop()
      // correr hasta el reposo (~300 ticks con alphaDecay default), sin timer ni animación
      const ticks = Math.ceil(Math.log(sim.alphaMin()) / Math.log(1 - sim.alphaDecay()))
      for (let i = 0; i < ticks; i++) sim.tick()
    }
    let minX = Infinity
    let minY = Infinity
    let maxX = -Infinity
    let maxY = -Infinity
    const out: PlacedNode[] = simNodes.map((s) => {
      const x = s.x ?? 0
      const y = s.y ?? 0
      minX = Math.min(minX, x - s.r)
      minY = Math.min(minY, y - s.r)
      maxX = Math.max(maxX, x + s.r)
      maxY = Math.max(maxY, y + s.r)
      return { key: s.key, node: s.node, x, y, deg: s.deg }
    })
    placed.push(out)
    mins.push({ x: minX - PAD, y: minY - PAD })
    boxes.push({ w: maxX - minX + 2 * PAD, h: maxY - minY + 2 * PAD })
  }

  // 2) empaquetar las cajas: FFDH (alto desc; desempate por ancho y orden de aparición).
  const order = boxes
    .map((b, i) => ({ b, i }))
    .sort((p, q) => q.b.h - p.b.h || q.b.w - p.b.w || p.i - q.i)
  const offsets = shelfPack(
    order.map((o) => o.b),
    GAP,
    TARGET_ASPECT,
  )

  // 3) trasladar cada componente a su celda y juntar el resultado.
  const sims: PlacedNode[] = []
  const bounds: Bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity }
  order.forEach((o, j) => {
    const dx = offsets[j].x - mins[o.i].x
    const dy = offsets[j].y - mins[o.i].y
    for (const p of placed[o.i]) {
      const r = baseRadius(p.node, p.deg)
      const node: PlacedNode = { ...p, x: p.x + dx, y: p.y + dy }
      sims.push(node)
      bounds.minX = Math.min(bounds.minX, node.x - r)
      bounds.minY = Math.min(bounds.minY, node.y - r)
      bounds.maxX = Math.max(bounds.maxX, node.x + r)
      bounds.maxY = Math.max(bounds.maxY, node.y + r)
    }
  })
  return { sims, byKey: new Map(sims.map((s) => [s.key, s])), bounds }
}
