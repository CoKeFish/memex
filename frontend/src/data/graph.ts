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
  sourceInboxIds: number[]
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
  pertenenciaReales: number
  contraparteReales: number
  highFanoutSkipped: number
}

export interface GraphClusterResult {
  detected: number
  newCandidates: number
  matchedSame: number
  matchedDrift: number
  memoSkipped: number
  deleted: number
  dissolved: number
}

export interface GraphClusterValidateResult {
  clusters: number
  confirmed: number
  rejected: number
  prunedMembers: number
  skipped: number
  errors: number
  llmCalls: number
  costUsd: number
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

interface NodeApiRow {
  slug: string
  id: number
  label: string
  kind: string
  source_inbox_ids: number[]
}

interface GraphApi {
  nodes: NodeApiRow[]
  edges: EdgeApiRow[]
}

function toNode(n: NodeApiRow): GraphNode {
  return { slug: n.slug, id: n.id, label: n.label, kind: n.kind, sourceInboxIds: n.source_inbox_ids ?? [] }
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

/** El grafo del usuario (GET /graph). `status` filtra aristas (pista|confirmed|rejected);
 *  `sourceInboxId` ENFOCA el subgrafo en lo que produjo ese correo (sus vértices + vecinos). */
export async function fetchGraph(status?: string, sourceInboxId?: number): Promise<GraphData> {
  const qs = new URLSearchParams()
  if (status) qs.set("status", status)
  if (sourceInboxId != null) qs.set("source_inbox_id", String(sourceInboxId))
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  const g = await apiGet<GraphApi>(`/graph${suffix}`)
  return { nodes: g.nodes.map(toNode), edges: g.edges.map(toEdge) }
}

/** Corre el paso determinista (POST /graph/build): materializa pistas + reales sobre lo guardado. */
export async function buildGraph(): Promise<GraphBuildResult> {
  const r = await apiPost<{
    cooccurrence_pistas: number
    afiliacion_reales: number
    pertenencia_reales?: number
    contraparte_reales?: number
    high_fanout_skipped: number
  }>("/graph/build")
  return {
    cooccurrencePistas: r.cooccurrence_pistas,
    afiliacionReales: r.afiliacion_reales,
    pertenenciaReales: r.pertenencia_reales ?? 0,
    contraparteReales: r.contraparte_reales ?? 0,
    highFanoutSkipped: r.high_fanout_skipped,
  }
}

/** Detecta los cúmulos (Louvain) y los reconcilia contra lo persistido (POST /graph/cluster, sin
 *  LLM). Idempotente: re-detectar la misma partición no cambia nada. */
export async function clusterGraph(): Promise<GraphClusterResult> {
  const r = await apiPost<{
    detected: number
    new_candidates: number
    matched_same: number
    matched_drift: number
    memo_skipped: number
    deleted: number
    dissolved: number
  }>("/graph/cluster")
  return {
    detected: r.detected,
    newCandidates: r.new_candidates,
    matchedSame: r.matched_same,
    matchedDrift: r.matched_drift,
    memoSkipped: r.memo_skipped,
    deleted: r.deleted,
    dissolved: r.dissolved,
  }
}

/** Valida con el LLM los cúmulos pendientes (POST /graph/cluster/validate): confirma/nombra/poda y
 *  materializa las aristas `miembro_de`. Usa el LLM → TIENE COSTO. Solo toca los pendientes. */
export async function validateClusters(): Promise<GraphClusterValidateResult> {
  const r = await apiPost<{
    clusters: number
    confirmed: number
    rejected: number
    pruned_members: number
    skipped: number
    errors: number
    llm_calls: number
    cost_usd: number
  }>("/graph/cluster/validate")
  return {
    clusters: r.clusters,
    confirmed: r.confirmed,
    rejected: r.rejected,
    prunedMembers: r.pruned_members,
    skipped: r.skipped,
    errors: r.errors,
    llmCalls: r.llm_calls,
    costUsd: r.cost_usd,
  }
}
