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

/** "Descartar" un remitente: crea una regla ignore (drop puro) — POST /quality/senders/discard. */
export async function discardSender(email: string): Promise<{ ruleId: number; created: boolean }> {
  const r = await apiPost<{ rule_id: number; created: boolean }>("/quality/senders/discard", {
    sender_email: email,
  })
  return { ruleId: r.rule_id, created: r.created }
}
