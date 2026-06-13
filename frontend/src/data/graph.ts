// Superficie del GRAFO de relaciones contra la API real. Vértices (proyección de los `mod_*`) +
// aristas (`relation_edges`). Cada arista lleva su PRODUCTOR (quién la formó) y DOS EJES:
// `provenance` (extracted = leído de la fuente / inferred = el LLM lo dedujo) × `verdict`
// (confirmed/rejected/ambiguous). `label` es la etiqueta canónica derivada de ambos
// (EXTRACTED/INFERRED/AMBIGUOUS/...) y `relation` la justificación corta. Como el resto de
// `@/data`: funciones async + transform snake_case → camelCase.

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
  /** Cómo lo sabemos: "extracted" (determinista) | "inferred" (el LLM lo dedujo). */
  provenance: string
  /** La decisión: "confirmed" | "rejected" | "ambiguous". */
  verdict: string
  /** Etiqueta canónica derivada (EXTRACTED/INFERRED/INFERRED REJECTED/AMBIGUOUS[(inferred)]). */
  label: string
  /** Justificación corta de la relación (la `relation` nombrada del LLM, o texto determinista). */
  relation: string
  /** Groundwork incremental (ADR-021): la arista cambió desde el último mantenimiento. */
  dirty: boolean
  confidence: number | null
  evidence: string
  /** TODOS los mensajes que generaron la pista de co-ocurrencia (no solo el primero del
   * `evidence`); vacío para aristas de otros productores. Mismo drill-down que los nodos. */
  sourceInboxIds: number[]
}

/** Etiqueta canónica de una arista derivada de los dos ejes — espejo de `edges.canonical_label`
 *  (backend). Las aristas reales ya la traen en `label`; las sintéticas del plegado la derivan. */
export function canonicalLabel(provenance: string, verdict: string): string {
  if (verdict === "confirmed") return provenance === "extracted" ? "EXTRACTED" : "INFERRED"
  if (verdict === "rejected") return provenance === "inferred" ? "INFERRED REJECTED" : "REJECTED"
  return provenance === "inferred" ? "AMBIGUOUS (inferred)" : "AMBIGUOUS"
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** Medio (email|chat|social) por id de inbox referenciado — para etiquetar «correo/chat/social
   * #N» sin otra llamada; un id ausente cae a «mensaje #N». */
  inboxKinds: Record<number, string>
}

export interface GraphBuildResult {
  cooccurrencePistas: number
  afiliacionReales: number
  pertenenciaReales: number
  contraparteReales: number
  participaReales: number
  chatSenders: number
  canales: number
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
  blobs: number
  groups: number
  created: number
  synced: number
  dissolved: number
  rejected: number
  promoted: number
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
  provenance: string
  verdict: string
  label: string
  relation: string
  dirty: boolean
  confidence: number | null
  evidence: string
  source_inbox_ids?: number[]
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
  inbox_kinds?: Record<number, string>
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
    provenance: e.provenance,
    verdict: e.verdict,
    label: e.label,
    relation: e.relation,
    dirty: e.dirty,
    confidence: e.confidence,
    evidence: e.evidence,
    sourceInboxIds: e.source_inbox_ids ?? [],
  }
}

/** El grafo del usuario (GET /graph). `verdict` filtra aristas (confirmed|rejected|ambiguous);
 *  `sourceInboxId` ENFOCA el subgrafo en lo que produjo ese mensaje (sus vértices + vecinos). */
export async function fetchGraph(verdict?: string, sourceInboxId?: number): Promise<GraphData> {
  const qs = new URLSearchParams()
  if (verdict) qs.set("verdict", verdict)
  if (sourceInboxId != null) qs.set("source_inbox_id", String(sourceInboxId))
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  const g = await apiGet<GraphApi>(`/graph${suffix}`)
  return { nodes: g.nodes.map(toNode), edges: g.edges.map(toEdge), inboxKinds: g.inbox_kinds ?? {} }
}

/** Corre el paso determinista (POST /graph/build): materializa pistas + reales sobre lo guardado. */
export async function buildGraph(): Promise<GraphBuildResult> {
  const r = await apiPost<{
    cooccurrence_pistas: number
    afiliacion_reales: number
    pertenencia_reales?: number
    contraparte_reales?: number
    participa_reales?: number
    chat_senders?: number
    canales?: number
    high_fanout_skipped: number
  }>("/graph/build")
  return {
    cooccurrencePistas: r.cooccurrence_pistas,
    afiliacionReales: r.afiliacion_reales,
    pertenenciaReales: r.pertenencia_reales ?? 0,
    contraparteReales: r.contraparte_reales ?? 0,
    participaReales: r.participa_reales ?? 0,
    chatSenders: r.chat_senders ?? 0,
    canales: r.canales ?? 0,
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

/** Parte con el LLM los blobs pendientes (POST /graph/cluster/validate): cada blob → N contextos
 *  (hijos confirmed), promueve sus pistas internas y materializa las aristas `miembro_de`. Usa el
 *  LLM → TIENE COSTO. Solo toca los pendientes. */
export async function validateClusters(): Promise<GraphClusterValidateResult> {
  const r = await apiPost<{
    blobs: number
    groups: number
    created: number
    synced: number
    dissolved: number
    rejected: number
    promoted: number
    skipped: number
    errors: number
    llm_calls: number
    cost_usd: number
  }>("/graph/cluster/validate")
  return {
    blobs: r.blobs,
    groups: r.groups,
    created: r.created,
    synced: r.synced,
    dissolved: r.dissolved,
    rejected: r.rejected,
    promoted: r.promoted,
    skipped: r.skipped,
    errors: r.errors,
    llmCalls: r.llm_calls,
    costUsd: r.cost_usd,
  }
}

export interface GraphConfirmResult {
  edges: number
  confirmedRecibo: number
  messages: number
  chatSkipped: number
  llmConfirmed: number
  llmRejected: number
  llmDejar: number
  gated: number
  summaries: number
  errors: number
  llmCalls: number
  costUsd: number
}

/** Confirmación por-mensaje de las co-ocurrencias ambiguas (POST /graph/confirm): metodología B
 *  (recibo a priori + LLM con compuerta alias-aware). Usa el LLM → TIENE COSTO. Monótono. */
export async function confirmCooccurrences(): Promise<GraphConfirmResult> {
  const r = await apiPost<{
    edges: number
    confirmed_recibo: number
    messages: number
    chat_skipped: number
    llm_confirmed: number
    llm_rejected: number
    llm_dejar: number
    gated: number
    summaries: number
    errors: number
    llm_calls: number
    cost_usd: number
  }>("/graph/confirm")
  return {
    edges: r.edges,
    confirmedRecibo: r.confirmed_recibo,
    messages: r.messages,
    chatSkipped: r.chat_skipped,
    llmConfirmed: r.llm_confirmed,
    llmRejected: r.llm_rejected,
    llmDejar: r.llm_dejar,
    gated: r.gated,
    summaries: r.summaries,
    errors: r.errors,
    llmCalls: r.llm_calls,
    costUsd: r.cost_usd,
  }
}

// --- Cronología / story de un cúmulo (GET /graph/clusters/:id/timeline) --------------------------- //

/** Un suceso fechado del cúmulo. `at` es ISO en hora local; `precision` ∈ datetime|date|inferred. */
export interface TimelineEvent {
  slug: string
  id: number
  kind: string
  label: string
  at: string
  precision: string
  sourceInboxIds: number[]
}

/** Un miembro sin fecha de evento (elenco/contexto: identidad, hábito). */
export interface TimelineActor {
  slug: string
  id: number
  kind: string
  label: string
  sourceInboxIds: number[]
}

export interface ClusterTimelineData {
  cluster: {
    id: number
    name: string
    description: string
    confidence: number | null
    memberCount: number
  }
  events: TimelineEvent[]
  actors: TimelineActor[]
  /** Medio (email|chat|social) por id de inbox referenciado por sucesos/elenco. */
  inboxKinds: Record<number, string>
}

interface TimelineEventApi {
  slug: string
  id: number
  kind: string
  label: string
  at: string
  precision: string
  source_inbox_ids: number[]
}

interface ClusterTimelineApi {
  cluster: {
    id: number
    name: string
    description: string
    confidence: number | null
    member_count: number
  }
  events: TimelineEventApi[]
  actors: Omit<TimelineEventApi, "at" | "precision">[]
  inbox_kinds?: Record<number, string>
}

/** Cronología de un cúmulo CONFIRMADO: sus sucesos fechados (ordenados) + el elenco (sin fecha). */
export async function fetchClusterTimeline(id: number): Promise<ClusterTimelineData> {
  const r = await apiGet<ClusterTimelineApi>(`/graph/clusters/${id}/timeline`)
  return {
    cluster: {
      id: r.cluster.id,
      name: r.cluster.name,
      description: r.cluster.description,
      confidence: r.cluster.confidence,
      memberCount: r.cluster.member_count,
    },
    events: r.events.map((e) => ({
      slug: e.slug,
      id: e.id,
      kind: e.kind,
      label: e.label,
      at: e.at,
      precision: e.precision,
      sourceInboxIds: e.source_inbox_ids ?? [],
    })),
    actors: r.actors.map((a) => ({
      slug: a.slug,
      id: a.id,
      kind: a.kind,
      label: a.label,
      sourceInboxIds: a.source_inbox_ids ?? [],
    })),
    inboxKinds: r.inbox_kinds ?? {},
  }
}
