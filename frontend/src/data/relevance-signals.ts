// Capa de SEÑALES del sistema UNIFICADO de relevancia (router `/relevance`): lectura agregada por
// remitente (ruido primero, determinista, sin LLM), el dial de COSTO por tier (batch/individual) y
// la cola de candidatos por PROCEDIMIENTO + la re-evaluación por el MOTOR ÚNICO (el juez del gate,
// no un segundo juez). Fusionada desde el ex-`/quality`. La marca manual por-mensaje vive en /datos.

import { apiDelete, apiGet, apiPost } from "@/lib/api"
import type { Tier } from "@/types/domain"

/** Tier como dial de COSTO sobre lo relevante. «No procesar» ya NO es un tier: es una regla `block`
 *  del gate (Bloquear remitente = `createGateRule({ effect: "block", … })`). */
export type CostTier = "batch" | "individual"

/**
 * Relevancia agregada de un remitente. `relevancePct` cuenta SOLO los mensajes que produjeron un
 * hecho de dominio; `summarizedOnly` (se resumió pero sin hecho) e `inert` (ni hecho ni resumen)
 * son buckets aparte para no lavar la señal.
 */
export interface SenderRelevance {
  senderKey: string
  senderLabel: string
  messages: number
  relevant: number
  summarizedOnly: number
  inert: number
  marked: number
  /** Email del remitente si es accionable (sender→tier es email-only en v1); null para chat/social. */
  email: string | null
  /** Tier forzado activo (batch/individual) o null. */
  overrideTier: string | null
  /** Fuente del remitente: email | chat | social | other (para filtrar la vista). */
  kind: string
  relevancePct: number | null
  lastAt: string | null
  tierMix: Record<string, number>
  volumeRatio: number | null
}

interface SenderRelevanceApi {
  sender_key: string
  sender_label: string
  messages: number
  relevant: number
  summarized_only: number
  inert: number
  marked: number
  email: string | null
  override_tier: string | null
  kind: string
  relevance_pct: number | null
  last_at: string | null
  tier_mix: Record<string, number>
  volume_ratio: number | null
}

interface SenderRelevanceListApi {
  items: SenderRelevanceApi[]
}

function toSender(it: SenderRelevanceApi): SenderRelevance {
  return {
    senderKey: it.sender_key,
    senderLabel: it.sender_label,
    messages: it.messages,
    relevant: it.relevant,
    summarizedOnly: it.summarized_only,
    inert: it.inert,
    marked: it.marked,
    email: it.email,
    overrideTier: it.override_tier,
    kind: it.kind,
    relevancePct: it.relevance_pct,
    lastAt: it.last_at,
    tierMix: it.tier_mix ?? {},
    volumeRatio: it.volume_ratio,
  }
}

/** Remitentes rankeados por relevancia (ruido primero) — GET /relevance/senders. */
export async function fetchSenderRelevance(limit = 200): Promise<SenderRelevance[]> {
  const data = await apiGet<SenderRelevanceListApi>(`/relevance/senders?limit=${limit}`)
  return data.items.map(toSender)
}

/** Dial de COSTO: fuerza el tier (batch/individual) de los mensajes futuros — POST /relevance/senders/tier. */
export async function setSenderTier(
  email: string,
  tier: CostTier = "batch",
  reason: string | null = null,
): Promise<void> {
  await apiPost("/relevance/senders/tier", { sender_email: email, tier, reason })
}

/** Quita el override de tier de un remitente — DELETE /relevance/senders/tier. */
export async function clearSenderTier(email: string): Promise<void> {
  await apiDelete<void>(`/relevance/senders/tier?sender_email=${encodeURIComponent(email)}`)
}

/** Override de tier por remitente (fila de sender_tier_overrides). */
export interface SenderTierOverride {
  senderEmail: string
  tier: Tier
  reason: string | null
  createdAt: string | null
  updatedAt: string | null
}

interface SenderTierOverrideApi {
  sender_email: string
  tier: string
  reason: string | null
  created_at: string | null
  updated_at: string | null
}

/** Overrides de tier del usuario (recientes primero) — GET /relevance/senders/tiers. */
export async function fetchSenderTiers(): Promise<SenderTierOverride[]> {
  const data = await apiGet<{ items: SenderTierOverrideApi[] }>("/relevance/senders/tiers")
  return data.items.map((it) => ({
    senderEmail: it.sender_email,
    tier: it.tier as Tier,
    reason: it.reason,
    createdAt: it.created_at,
    updatedAt: it.updated_at,
  }))
}

/** Candidato a (re)evaluar que armó un PROCEDIMIENTO determinista (no acciona solo). */
export interface RelevanceCandidate {
  /** Qué procedimiento lo detectó (ej. `fact_count` = procesado sin hecho). */
  procedure: string
  /** Unidad del seam por-ingestor (hoy `sender`). */
  unitType: string
  senderKey: string
  senderLabel: string
  email: string | null
  messages: number
  relevant: number
  inert: number
  relevancePct: number | null
  score: number
  status: string
  sampleInboxIds: number[]
}

interface RelevanceCandidateApi {
  procedure: string
  unit_type: string
  sender_key: string
  sender_label: string
  email: string | null
  messages: number
  relevant: number
  inert: number
  relevance_pct: number | null
  score: number
  status: string
  snapshot: { sample_inbox_ids?: number[] }
}

function toCandidate(c: RelevanceCandidateApi): RelevanceCandidate {
  return {
    procedure: c.procedure,
    unitType: c.unit_type,
    senderKey: c.sender_key,
    senderLabel: c.sender_label,
    email: c.email,
    messages: c.messages,
    relevant: c.relevant,
    inert: c.inert,
    relevancePct: c.relevance_pct,
    score: c.score,
    status: c.status,
    sampleInboxIds: c.snapshot?.sample_inbox_ids ?? [],
  }
}

/** Candidatos por procedimiento (ruido primero) — GET /relevance/candidates. `procedure` filtra. */
export async function fetchCandidates(
  status = "open",
  procedure?: string,
): Promise<RelevanceCandidate[]> {
  const qs = new URLSearchParams({ status })
  if (procedure) qs.set("procedure", procedure)
  const data = await apiGet<{ items: RelevanceCandidateApi[] }>(`/relevance/candidates?${qs}`)
  return data.items.map(toCandidate)
}

/** Mueve el estado de un candidato (confirmed/dismissed) — POST /relevance/candidates/status. */
export async function setCandidateStatus(
  senderKey: string,
  status: string,
  procedure?: string,
): Promise<void> {
  await apiPost("/relevance/candidates/status", { sender_key: senderKey, status, procedure })
}

/** Conteo de veredictos al re-evaluar la muestra de un candidato por el motor único. */
export interface ReevaluateResult {
  messages: number
  relevant: number
  notRelevant: number
  insufficient: number
}

/** Re-evalúa la muestra de un candidato por el MOTOR ÚNICO (el juez del gate + intereses; corre el
 *  gate sobre la muestra con force) — POST /relevance/candidates/reevaluate. */
export async function reevaluateCandidate(
  senderKey: string,
  procedure?: string,
): Promise<ReevaluateResult> {
  const r = await apiPost<{
    messages: number
    relevant: number
    not_relevant: number
    insufficient: number
  }>("/relevance/candidates/reevaluate", { sender_key: senderKey, procedure })
  return {
    messages: r.messages,
    relevant: r.relevant,
    notRelevant: r.not_relevant,
    insufficient: r.insufficient,
  }
}
