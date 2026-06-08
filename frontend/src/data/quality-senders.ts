// Relevancia por remitente contra la API real: GET /quality/senders. Lectura determinista del
// sistema de calidad (ruido primero), sin LLM. La marca manual y las acciones llegan en fases
// posteriores.

import { apiGet } from "@/lib/api"

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
