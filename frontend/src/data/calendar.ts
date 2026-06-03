// Superficie del dominio CALENDAR contra la API real (no mocks). Como `finance.ts`/`email.ts`:
// funciones async + transforms snake_case → camelCase. La vista `/calendario` consume la capa
// consolidada + dedup + conflictos + sync, todo de SOLO LECTURA (GET /calendar/*).

import { apiGet } from "@/lib/api"
import type {
  CalendarConflict,
  CalendarEventLite,
  CalendarOrigin,
  CalendarOutcome,
  CalendarRawMember,
  CalendarSyncRun,
  ConsolidatedEvent,
  ConsolidatedEventLite,
  DedupDecision,
  ProviderAccount,
} from "@/types/domain"

// ---- Shapes crudos del API (snake_case) -------------------------------------------------------

interface RawMemberApi {
  id: number
  origin: CalendarOrigin
  provider: string | null
  source_inbox_ids: number[]
  evidence: string
  processing_outcome: CalendarOutcome
  is_winner: boolean
}

interface ConsolidatedApi {
  id: number
  title: string
  starts_on: string
  ends_on: string | null
  start_time: string | null
  end_time: string | null
  location: string
  description: string
  member_count: number
  origins: CalendarOrigin[]
  protected: boolean
  priority_rank: number
  members: RawMemberApi[]
}

interface EventLiteApi {
  id: number
  title: string
  starts_on: string
  start_time: string | null
  location: string
  origin: CalendarOrigin
  provider: string | null
}

interface DedupDecisionApi {
  id: number
  a: EventLiteApi
  b: EventLiteApi
  reason: string
  score: number | null
  status: DedupDecision["status"]
  decided_by: DedupDecision["decidedBy"]
  confidence: number | null
  rationale: string | null
  decided_at: string | null
}

interface ConsolidatedLiteApi {
  id: number
  title: string
  starts_on: string
  ends_on: string | null
  start_time: string | null
  end_time: string | null
  location: string
  priority_rank: number
  protected: boolean
}

interface ConflictApi {
  id: number
  a: ConsolidatedLiteApi
  b: ConsolidatedLiteApi
  reason: string
  status: CalendarConflict["status"]
  created_at: string
}

interface SyncRunApi {
  id: number
  account: string
  direction: CalendarSyncRun["direction"]
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  dedup_pairs: number
  errors: number
  status: CalendarSyncRun["status"]
  started_at: string
  finished_at: string | null
}

interface ProviderAccountApi {
  id: number
  provider: string
  account_label: string
  calendar_id: string
  last_sync_at: string | null
  token_path_env: string
  enabled: boolean
  write_back: boolean
  sync_token_present: boolean
}

interface ListApi<T> {
  items: T[]
  next_cursor: number | null
}

// ---- Transforms ------------------------------------------------------------------------------

/** Las horas cruzan como TIME "HH:MM:SS"; la UI las quiere "HH:MM". */
function hhmm(t: string | null): string | null {
  return t ? t.slice(0, 5) : null
}

function toMember(m: RawMemberApi): CalendarRawMember {
  return {
    id: m.id,
    origin: m.origin,
    provider: m.provider,
    sourceInboxIds: m.source_inbox_ids,
    evidence: m.evidence,
    processingOutcome: m.processing_outcome,
    isWinner: m.is_winner,
  }
}

function toConsolidatedEvent(r: ConsolidatedApi): ConsolidatedEvent {
  return {
    id: r.id,
    title: r.title,
    startsOn: r.starts_on,
    endsOn: r.ends_on,
    startTime: hhmm(r.start_time),
    endTime: hhmm(r.end_time),
    location: r.location,
    description: r.description,
    memberCount: r.member_count,
    origins: r.origins,
    protected: r.protected,
    priorityRank: r.priority_rank,
    members: r.members.map(toMember),
  }
}

function toEventLite(e: EventLiteApi): CalendarEventLite {
  return {
    id: e.id,
    title: e.title,
    startsOn: e.starts_on,
    startTime: hhmm(e.start_time),
    location: e.location,
    origin: e.origin,
    provider: e.provider,
  }
}

function toConsolidatedLite(e: ConsolidatedLiteApi): ConsolidatedEventLite {
  return {
    id: e.id,
    title: e.title,
    startsOn: e.starts_on,
    endsOn: e.ends_on,
    startTime: hhmm(e.start_time),
    endTime: hhmm(e.end_time),
    location: e.location,
    priorityRank: e.priority_rank,
    protected: e.protected,
  }
}

function toDedupDecision(r: DedupDecisionApi): DedupDecision {
  return {
    id: r.id,
    a: toEventLite(r.a),
    b: toEventLite(r.b),
    reason: r.reason,
    score: r.score,
    status: r.status,
    decidedBy: r.decided_by,
    confidence: r.confidence,
    rationale: r.rationale,
    decidedAt: r.decided_at,
  }
}

function toConflict(r: ConflictApi): CalendarConflict {
  return {
    id: r.id,
    a: toConsolidatedLite(r.a),
    b: toConsolidatedLite(r.b),
    reason: r.reason,
    status: r.status,
    createdAt: r.created_at,
  }
}

function toSyncRun(r: SyncRunApi): CalendarSyncRun {
  return {
    id: r.id,
    account: r.account,
    direction: r.direction,
    pulled: r.pulled,
    created: r.created,
    modified: r.modified,
    deleted: r.deleted,
    unchanged: r.unchanged,
    dedupPairs: r.dedup_pairs,
    errors: r.errors,
    status: r.status,
    startedAt: r.started_at,
    finishedAt: r.finished_at,
  }
}

function toProviderAccount(r: ProviderAccountApi): ProviderAccount {
  // El cursor delta nunca cruza el wire: derivamos el estado de su presencia.
  const tokenState: ProviderAccount["tokenState"] = r.sync_token_present
    ? "delta"
    : r.last_sync_at
      ? "full-resync"
      : "never"
  return {
    id: r.id,
    provider: r.provider,
    accountLabel: r.account_label,
    calendarId: r.calendar_id,
    lastSyncAt: r.last_sync_at,
    syncTokenMasked: null, // la API no expone el token (ADR-001)
    tokenPathEnv: r.token_path_env,
    enabled: r.enabled,
    writeBack: r.write_back,
    tokenState,
  }
}

// ---- Fetchers --------------------------------------------------------------------------------

/**
 * Todos los eventos consolidados del usuario (GET /calendar/events), paginando por cursor igual que
 * `fetchFinanceExpenses`. El calendario mensual/agenda filtran por fecha en el cliente.
 */
export async function fetchCalendarEvents(max = 5000): Promise<ConsolidatedEvent[]> {
  const pageSize = 500
  const out: ConsolidatedEvent[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams({ limit: String(pageSize) })
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<ListApi<ConsolidatedApi>>(`/calendar/events?${qs.toString()}`)
    out.push(...page.items.map(toConsolidatedEvent))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}

export async function fetchDedupDecisions(): Promise<DedupDecision[]> {
  const page = await apiGet<ListApi<DedupDecisionApi>>("/calendar/dedup-candidates?limit=500")
  return page.items.map(toDedupDecision)
}

export async function fetchCalendarConflicts(): Promise<CalendarConflict[]> {
  const page = await apiGet<ListApi<ConflictApi>>("/calendar/conflicts?limit=500")
  return page.items.map(toConflict)
}

export async function fetchCalendarSyncRuns(): Promise<CalendarSyncRun[]> {
  const page = await apiGet<ListApi<SyncRunApi>>("/calendar/sync-runs?limit=100")
  return page.items.map(toSyncRun)
}

export async function fetchCalendarProviderAccounts(): Promise<ProviderAccount[]> {
  const page = await apiGet<{ items: ProviderAccountApi[] }>("/calendar/provider-accounts")
  return page.items.map(toProviderAccount)
}
