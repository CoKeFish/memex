// Plegado del grafo por CÚMULO confirmado: los miembros de un cúmulo NO expandido se ocultan y
// sus aristas hacia afuera se re-rutean al nodo cúmulo, agregadas con contador. Función pura,
// client-side (el payload de GET /graph ya trae los cúmulos y sus `miembro_de`). Genérico sobre
// `miembro_de`: un cúmulo miembro de otro cúmulo (futuro: cúmulos jerárquicos) anida solo — el
// representante es el ancestro plegado MÁS EXTERNO.

import type { GraphEdge, GraphNode } from "@/data/graph"
import { nodeKey } from "@/lib/graph-layout"

export interface CollapsedEdge extends GraphEdge {
  /** Cuántas aristas reales agrega esta sintética (solo en `relationType: "agregada"`). */
  aggregateCount?: number
}

export interface CollapsedGraph {
  nodes: GraphNode[]
  edges: CollapsedEdge[]
}

const CUMULO = "cumulo"

/** Pliega los cúmulos cuyo id NO está en `expandedIds`. Los `miembro_de` se toman solo entre
 * nodos PRESENTES (así el plegado respeta los filtros aplicados antes: si la leyenda oculta los
 * cúmulos, no hay membresía y no se pliega nada). Aristas internas al plegado se descartan; las
 * re-ruteadas se agregan por par canónico en una arista sintética determinista (id negativo en
 * orden de clave; `status` confirmed si alguna constituyente lo era). */
export function collapseClusters(
  nodes: GraphNode[],
  edges: GraphEdge[],
  expandedIds: ReadonlySet<number>,
): CollapsedGraph {
  const present = new Set(nodes.map((n) => nodeKey(n.slug, n.id)))
  // miembro → id del cúmulo del que es miembro (las membresías del partidor son disjuntas).
  const parent = new Map<string, number>()
  for (const e of edges) {
    if (e.relationType !== "miembro_de" || e.dstSlug !== CUMULO) continue
    const member = nodeKey(e.srcSlug, e.srcId)
    if (!present.has(member) || !present.has(nodeKey(CUMULO, e.dstId))) continue
    parent.set(member, e.dstId)
  }
  if (parent.size === 0) return { nodes, edges }

  // Representante: el ancestro PLEGADO más externo de la cadena de membresía (o el propio nodo).
  const repCache = new Map<string, string>()
  const rep = (key: string): string => {
    const hit = repCache.get(key)
    if (hit !== undefined) return hit
    let out = key
    const seen = new Set<string>([key])
    let cur = key
    for (let pid = parent.get(cur); pid !== undefined; pid = parent.get(cur)) {
      cur = nodeKey(CUMULO, pid)
      if (seen.has(cur)) break // guard: una membresía cíclica no debe colgar el render
      seen.add(cur)
      if (!expandedIds.has(pid)) out = cur // plegado más EXTERNO gana
    }
    repCache.set(key, out)
    return out
  }

  const outNodes = nodes.filter((n) => rep(nodeKey(n.slug, n.id)) === nodeKey(n.slug, n.id))
  const passthrough: CollapsedEdge[] = []
  const agg = new Map<string, { count: number; confirmed: boolean }>()
  for (const e of edges) {
    const ka = nodeKey(e.srcSlug, e.srcId)
    const kb = nodeKey(e.dstSlug, e.dstId)
    const ra = rep(ka)
    const rb = rep(kb)
    if (ra === rb) continue // interna al plegado (incluye sus miembro_de)
    if (ra === ka && rb === kb) {
      passthrough.push(e)
      continue
    }
    const [a, b] = ra < rb ? [ra, rb] : [rb, ra]
    const k = `${a}|${b}`
    const cur = agg.get(k) ?? { count: 0, confirmed: false }
    cur.count += 1
    cur.confirmed = cur.confirmed || e.status === "confirmed"
    agg.set(k, cur)
  }

  // Sintéticas deterministas: id negativo en orden de clave canónica (misma entrada → mismos ids).
  const synthetic: CollapsedEdge[] = [...agg.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([k, v], i) => {
      const [a, b] = k.split("|")
      const ha = a.lastIndexOf("#")
      const hb = b.lastIndexOf("#")
      return {
        id: -(i + 1),
        srcSlug: a.slice(0, ha),
        srcId: Number(a.slice(ha + 1)),
        dstSlug: b.slice(0, hb),
        dstId: Number(b.slice(hb + 1)),
        relationType: "agregada",
        producer: "colapso",
        status: v.confirmed ? "confirmed" : "pista",
        confidence: null,
        evidence: "",
        sourceInboxIds: [],
        aggregateCount: v.count,
      }
    })
  return { nodes: outNodes, edges: [...passthrough, ...synthetic] }
}
