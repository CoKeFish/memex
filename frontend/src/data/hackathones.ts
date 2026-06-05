// Superficie de HACKATONES contra la API real (no mocks). Como `finance.ts`: funciones async + un
// transform snake_case → camelCase. El dashboard trae las filas crudas y ordena/filtra en el
// cliente.

import { apiGet } from "@/lib/api"
import type { Hackathon, HackathonModality } from "@/types/domain"

interface HackathonApiRow {
  id: number
  name: string
  starts_on: string | null
  ends_on: string | null
  registration_deadline: string | null
  modality: string
  location: string
  url: string
  organizer: string
  technologies: string
  prizes: string
  requirements: string
  description: string
  evidence: string
  source_inbox_ids: number[]
  created_at: string
}

interface HackathonApiList {
  items: HackathonApiRow[]
  next_cursor: number | null
}

function toHackathon(r: HackathonApiRow): Hackathon {
  return {
    id: r.id,
    name: r.name,
    startsOn: r.starts_on,
    endsOn: r.ends_on,
    registrationDeadline: r.registration_deadline,
    modality: r.modality as HackathonModality,
    location: r.location,
    url: r.url,
    organizer: r.organizer,
    technologies: r.technologies,
    prizes: r.prizes,
    requirements: r.requirements,
    description: r.description,
    evidence: r.evidence,
    sourceInboxIds: r.source_inbox_ids,
    createdAt: r.created_at,
  }
}

export interface FetchHackathonesOpts {
  modality?: string
  /** starts_on >= since (YYYY-MM-DD) */
  since?: string
  /** starts_on < until (YYYY-MM-DD) */
  until?: string
  /** Tope total de filas a traer (paginando). */
  max?: number
}

/**
 * Todos los hackatones del usuario (GET /hackathones/events), paginando por cursor igual que
 * `fetchFinanceTransactions`. El dashboard ordena/filtra en el cliente, así que por defecto trae todo.
 */
export async function fetchHackathones(opts?: FetchHackathonesOpts): Promise<Hackathon[]> {
  const max = opts?.max ?? 5000
  const pageSize = 500
  const out: Hackathon[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams()
    if (opts?.modality) qs.set("modality", opts.modality)
    if (opts?.since) qs.set("since", opts.since)
    if (opts?.until) qs.set("until", opts.until)
    qs.set("limit", String(pageSize))
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<HackathonApiList>(`/hackathones/events?${qs.toString()}`)
    out.push(...page.items.map(toHackathon))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}
