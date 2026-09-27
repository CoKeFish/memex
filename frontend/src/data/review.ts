// Cola de revisión (/revision) contra datos reales: dead-letter (router /review) + conflictos de
// calendario (router /calendar). Reemplaza el mock getReviewItems(). El dedup de calendario vive en
// /calendar (su decisión es un slice de ese módulo) y no se duplica acá.

import { fetchCalendarConflicts } from "@/data/calendar"
import { apiGet, apiPost } from "@/lib/api"
import type { FailureStage, ReviewItem, WorkItemFailure } from "@/types/domain"

interface DeadLetterApi {
  id: number
  stage: FailureStage
  inbox_id: number
  attempts: number
  last_error: string | null
  status: "failing" | "review"
  created_at: string
  updated_at: string
  preview: string
}

function toDeadLetter(d: DeadLetterApi): WorkItemFailure {
  return {
    id: d.id,
    stage: d.stage,
    inboxId: d.inbox_id,
    attempts: d.attempts,
    lastError: d.last_error,
    status: d.status,
    createdAt: d.created_at,
    updatedAt: d.updated_at,
    preview: d.preview,
  }
}

/** Items reales de la cola: dead-letter (summarize/extract) + conflictos de calendario pendientes. */
export async function fetchReviewItems(): Promise<ReviewItem[]> {
  const [dlRows, conflicts] = await Promise.all([
    apiGet<DeadLetterApi[]>("/review/dead-letter"),
    fetchCalendarConflicts(),
  ])
  const items: ReviewItem[] = []
  for (const d of dlRows.map(toDeadLetter)) {
    items.push({ id: `dl-${d.stage}-${d.inboxId}`, kind: "dead-letter", at: d.updatedAt, deadLetter: d })
  }
  for (const c of conflicts.filter((c) => c.status === "pending")) {
    items.push({ id: `cf-${c.id}`, kind: "conflict", at: c.createdAt, conflict: c })
  }
  return items.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())
}

/** Reencola un dead-letter: lo saca de revisión → vuelve al work-set (reintento en la próxima corrida). */
export async function requeueDeadLetter(stage: FailureStage, inboxId: number): Promise<void> {
  await apiPost<{ ok: boolean }>(`/review/dead-letter/${stage}/${inboxId}/requeue`)
}
