// Feedback manual acumulado (gestión/calibración) contra la API real: GET /feedback + cambio de
// estado (POST /feedback/{inboxId}/status). La captura por-mensaje vive en `reportFeedback` (./email).

import { apiGet, apiPost } from "@/lib/api"
import type { FeedbackKind } from "@/types/domain"

export type FeedbackStatus = "open" | "reviewed" | "dismissed"
export type FeedbackStatusFilter = FeedbackStatus | "all"

/** Un feedback con el contexto de su mensaje, para la vista de calibración. */
export interface FeedbackEntry {
  inboxId: number
  kinds: FeedbackKind[]
  note: string | null
  metadata?: Record<string, unknown>
  status: FeedbackStatus
  createdAt: string | null
  updatedAt: string | null
  subject: string | null
  fromEmail: string | null
  tier: string | null
}

interface FeedbackListItemApi {
  inbox_id: number
  kinds: string[]
  note: string | null
  metadata?: Record<string, unknown>
  status: string
  created_at?: string | null
  updated_at?: string | null
  subject?: string | null
  from_email?: string | null
  tier?: string | null
}

interface FeedbackListApi {
  items: FeedbackListItemApi[]
}

function toEntry(it: FeedbackListItemApi): FeedbackEntry {
  return {
    inboxId: it.inbox_id,
    kinds: it.kinds as FeedbackKind[],
    note: it.note ?? null,
    metadata: it.metadata,
    status: it.status as FeedbackStatus,
    createdAt: it.created_at ?? null,
    updatedAt: it.updated_at ?? null,
    subject: it.subject ?? null,
    fromEmail: it.from_email ?? null,
    tier: it.tier ?? null,
  }
}

/** Lista el feedback acumulado del usuario, filtrando por estado — GET /feedback. */
export async function fetchFeedback(status: FeedbackStatusFilter = "open"): Promise<FeedbackEntry[]> {
  const data = await apiGet<FeedbackListApi>(`/feedback?status=${status}`)
  return data.items.map(toEntry)
}

/** Mueve el estado de un feedback (revisado/descartado/reabrir) — POST /feedback/{inboxId}/status. */
export async function setFeedbackStatus(inboxId: number, status: FeedbackStatus): Promise<void> {
  await apiPost(`/feedback/${inboxId}/status`, { status })
}
