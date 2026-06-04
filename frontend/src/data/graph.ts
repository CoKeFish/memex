// Superficie del GRAFO de relaciones contra la API real. Vértices (proyección de los `mod_*`) +
// aristas (`relation_edges`). Una arista lleva su PRODUCTOR (quién la formó) y su NIVEL `status`:
// "pista" (señal determinista SIN vouchar, p.ej. co-ocurrencia) vs "confirmed" (REAL, vouchada).
// Como el resto de `@/data`: funciones async + transform snake_case → camelCase.

import { apiGet, apiPost } from "@/lib/api"

export interface GraphNode {
  slug: string
  id: number
  label: string
  kind: string
}

export interface GraphEdge {
  id: number
  srcSlug: string
  srcId: number
  dstSlug: string
  dstId: number
  relationType: string
  producer: string
  status: string
  confidence: number | null
  evidence: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphBuildResult {
  cooccurrencePistas: number
  afiliacionReales: number
  highFanoutSkipped: number
}

interface EdgeApiRow {
  id: number
  src_slug: string
  src_id: number
  dst_slug: string
  dst_id: number
  relation_type: string
  producer: string
  status: string
  confidence: number | null
  evidence: string
}

interface GraphApi {
  nodes: GraphNode[]
  edges: EdgeApiRow[]
}

function toEdge(e: EdgeApiRow): GraphEdge {
  return {
    id: e.id,
    srcSlug: e.src_slug,
    srcId: e.src_id,
    dstSlug: e.dst_slug,
    dstId: e.dst_id,
    relationType: e.relation_type,
    producer: e.producer,
    status: e.status,
    confidence: e.confidence,
    evidence: e.evidence,
  }
}

/** El grafo del usuario (GET /graph). `status` opcional filtra aristas: pista|confirmed|rejected. */
export async function fetchGraph(status?: string): Promise<GraphData> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ""
  const g = await apiGet<GraphApi>(`/graph${qs}`)
  return { nodes: g.nodes, edges: g.edges.map(toEdge) }
}

/** Corre el paso determinista (POST /graph/build): materializa pistas + reales sobre lo guardado. */
export async function buildGraph(): Promise<GraphBuildResult> {
  const r = await apiPost<{
    cooccurrence_pistas: number
    afiliacion_reales: number
    high_fanout_skipped: number
  }>("/graph/build")
  return {
    cooccurrencePistas: r.cooccurrence_pistas,
    afiliacionReales: r.afiliacion_reales,
    highFanoutSkipped: r.high_fanout_skipped,
  }
}
