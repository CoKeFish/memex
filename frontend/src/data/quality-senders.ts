// Relevancia por remitente contra la API real: GET /quality/senders. Lectura determinista del
// sistema de calidad (ruido primero), sin LLM. La marca manual y las acciones llegan en fases
// posteriores.

import { apiDelete, apiGet, apiPost } from "@/lib/api"

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
  /** Tier forzado activo ("no procesar"/"muted") o null. */
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

/** Remitentes rankeados por relevancia (ruido primero) — GET /quality/senders. */
export async function fetchSenderRelevance(limit = 200): Promise<SenderRelevance[]> {
  const data = await apiGet<SenderRelevanceListApi>(`/quality/senders?limit=${limit}`)
  return data.items.map(toSender)
}

/** "No procesar" un remitente: fuerza el tier de sus mensajes futuros — POST /quality/senders/tier. */
export async function setSenderTier(
  email: string,
  tier = "blacklist",
  reason: string | null = null,
): Promise<void> {
  await apiPost("/quality/senders/tier", { sender_email: email, tier, reason })
}

/** Quita el override de tier de un remitente — DELETE /quality/senders/tier. */
export async function clearSenderTier(email: string): Promise<void> {
  await apiDelete<void>(`/quality/senders/tier?sender_email=${encodeURIComponent(email)}`)
}

/** Candidato a filtrar detectado por el job (remitente email ruidoso). */
export interface RelevanceCandidate {
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
  llmVerdict: { isRelevant: boolean; confidence: number; reason: string } | null
}

interface RelevanceCandidateApi {
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
  llm_verdict: { is_relevant: boolean; confidence: number; reason: string } | null
}

function toCandidate(c: RelevanceCandidateApi): RelevanceCandidate {
  return {
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
    llmVerdict: c.llm_verdict
      ? {
          isRelevant: c.llm_verdict.is_relevant,
          confidence: c.llm_verdict.confidence,
          reason: c.llm_verdict.reason,
        }
      : null,
  }
}

/** Candidatos a filtrar detectados por el job — GET /quality/candidates. */
export async function fetchCandidates(status = "open"): Promise<RelevanceCandidate[]> {
  const data = await apiGet<{ items: RelevanceCandidateApi[] }>(
    `/quality/candidates?status=${status}`,
  )
  return data.items.map(toCandidate)
}

/** Mueve el estado de un candidato (confirmed/dismissed) — POST /quality/candidates/status. */
export async function setCandidateStatus(senderKey: string, status: string): Promise<void> {
  await apiPost("/quality/candidates/status", { sender_key: senderKey, status })
}

/** Juez LLM de relevancia (zona gris) para un candidato — POST /quality/candidates/judge. */
export async function judgeSender(
  senderKey: string,
): Promise<{ isRelevant: boolean; confidence: number; reason: string }> {
  const r = await apiPost<{ is_relevant: boolean; confidence: number; reason: string }>(
    "/quality/candidates/judge",
    { sender_key: senderKey },
  )
  return { isRelevant: r.is_relevant, confidence: r.confidence, reason: r.reason }
}
