// Gate de relevancia por intereses personales (correos) contra la API real (/relevance).
// El portero que corre ANTES de resumen/extracción: settings (apagado por default), CRUD de
// intereses, reglas deterministas (con reporte de dry run) y la cola de revisión manual.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

// ---- Settings ----------------------------------------------------------------------------------

export type GateMode = "per_window" | "per_message"
export type GateProvider = "anthropic" | "codex"

export interface GateSettings {
  enabled: boolean
  mode: GateMode
  model: string
  /** Umbral de acumulación de la minería: no-relevantes por remitente para entrar al análisis. */
  mining_min_messages: number
  /** codex = suscripción vía `codex exec`: solo host-side y sin métricas de costo. */
  provider: GateProvider
  codex_model: string | null
}

/** Settings del gate — GET /relevance/settings (sin fila → defaults apagados). */
export async function fetchGateSettings(): Promise<GateSettings> {
  return apiGet<GateSettings>("/relevance/settings")
}

/** Patch parcial de los settings — PATCH /relevance/settings. */
export async function patchGateSettings(patch: Partial<GateSettings>): Promise<GateSettings> {
  return apiPatch<GateSettings>("/relevance/settings", patch)
}

// ---- Intereses ---------------------------------------------------------------------------------

export interface PersonalInterest {
  id: number
  text: string
  enabled: boolean
  createdAt: string
  updatedAt: string
}

interface InterestApi {
  id: number
  text: string
  enabled: boolean
  created_at: string
  updated_at: string
}

function toInterest(it: InterestApi): PersonalInterest {
  return {
    id: it.id,
    text: it.text,
    enabled: it.enabled,
    createdAt: it.created_at,
    updatedAt: it.updated_at,
  }
}

/** Intereses del usuario (orden estable) — GET /relevance/interests. */
export async function fetchInterests(): Promise<PersonalInterest[]> {
  const data = await apiGet<{ items: InterestApi[] }>("/relevance/interests")
  return data.items.map(toInterest)
}

/** Alta de un interés — POST /relevance/interests (409 si ya existe). */
export async function createInterest(text: string): Promise<PersonalInterest> {
  return toInterest(await apiPost<InterestApi>("/relevance/interests", { text }))
}

/** Patch parcial de un interés (texto y/o enabled) — PATCH /relevance/interests/{id}. */
export async function patchInterest(
  id: number,
  patch: { text?: string; enabled?: boolean },
): Promise<PersonalInterest> {
  return toInterest(await apiPatch<InterestApi>(`/relevance/interests/${id}`, patch))
}

/** Borra un interés — DELETE /relevance/interests/{id}. */
export async function deleteInterest(id: number): Promise<void> {
  await apiDelete<void>(`/relevance/interests/${id}`)
}

// ---- Reglas ------------------------------------------------------------------------------------

export type GateRuleKind = "sender_email" | "sender_domain" | "subject_contains" | "list_id"
export type GateRuleStatus = "active" | "disabled" | "rejected"

/** Reporte del dry run de una regla contra el histórico (la auditoría de su activación). */
export interface DryRunReport {
  matched: number
  matchedRelevant: number
  matchedNotRelevant: number
  matchedUnverdicted: number
  relevantSampleIds: number[]
  passes: boolean
}

export interface GateRule {
  id: number
  kind: GateRuleKind
  pattern: string
  status: GateRuleStatus
  proposedBy: "llm" | "manual"
  rationale: string
  dryRunReport: DryRunReport | null
  model: string | null
  activatedAt: string | null
  deactivatedAt: string | null
  createdAt: string
  updatedAt: string
}

interface DryRunReportApi {
  matched?: number
  matched_relevant?: number
  matched_not_relevant?: number
  matched_unverdicted?: number
  relevant_sample_ids?: number[]
  passes?: boolean
}

interface GateRuleApi {
  id: number
  kind: GateRuleKind
  pattern: string
  status: GateRuleStatus
  proposed_by: "llm" | "manual"
  rationale: string
  dry_run_report: DryRunReportApi
  model: string | null
  activated_at: string | null
  deactivated_at: string | null
  created_at: string
  updated_at: string
}

function toReport(r: DryRunReportApi): DryRunReport | null {
  if (r == null || r.matched === undefined) return null
  return {
    matched: r.matched,
    matchedRelevant: r.matched_relevant ?? 0,
    matchedNotRelevant: r.matched_not_relevant ?? 0,
    matchedUnverdicted: r.matched_unverdicted ?? 0,
    relevantSampleIds: r.relevant_sample_ids ?? [],
    passes: r.passes ?? false,
  }
}

function toRule(it: GateRuleApi): GateRule {
  return {
    id: it.id,
    kind: it.kind,
    pattern: it.pattern,
    status: it.status,
    proposedBy: it.proposed_by,
    rationale: it.rationale,
    dryRunReport: toReport(it.dry_run_report),
    model: it.model,
    activatedAt: it.activated_at,
    deactivatedAt: it.deactivated_at,
    createdAt: it.created_at,
    updatedAt: it.updated_at,
  }
}

/** Reglas del gate (todas o por status) — GET /relevance/rules. */
export async function fetchGateRules(status = "all"): Promise<GateRule[]> {
  const data = await apiGet<{ items: GateRuleApi[] }>(`/relevance/rules?status=${status}`)
  return data.items.map(toRule)
}

/** Alta manual de una regla (corre dry run; 422 con el reporte si atraparía un relevante). */
export async function createGateRule(
  kind: GateRuleKind,
  pattern: string,
  rationale = "",
): Promise<GateRule> {
  return toRule(await apiPost<GateRuleApi>("/relevance/rules", { kind, pattern, rationale }))
}

/** Toggle reversible de una regla — PATCH /relevance/rules/{id}. */
export async function patchGateRule(
  id: number,
  status: "active" | "disabled",
): Promise<GateRule> {
  return toRule(await apiPatch<GateRuleApi>(`/relevance/rules/${id}`, { status }))
}

export interface MineRulesResult {
  senders: number
  proposed: number
  activated: number
  rejected: number
  skipped: number
  costUsd: number
}

/** Minería on-demand de reglas (1 llamada LLM + dry run por propuesta) — POST /relevance/rules/mine. */
export async function mineGateRules(): Promise<MineRulesResult> {
  const r = await apiPost<{
    senders: number
    proposed: number
    activated: number
    rejected: number
    skipped: number
    cost_usd: number
  }>("/relevance/rules/mine")
  return {
    senders: r.senders,
    proposed: r.proposed,
    activated: r.activated,
    rejected: r.rejected,
    skipped: r.skipped,
    costUsd: r.cost_usd,
  }
}

// ---- Cola de revisión manual ---------------------------------------------------------------------

export interface ReviewItem {
  inboxId: number
  occurredAt: string
  fromEmail: string | null
  subject: string | null
  snippet: string
  reason: string
  createdAt: string
}

interface ReviewItemApi {
  inbox_id: number
  occurred_at: string
  from_email: string | null
  subject: string | null
  snippet: string
  reason: string
  created_at: string
}

/** Correos con veredicto `insufficient` esperando decisión — GET /relevance/review. */
export async function fetchReviewQueue(limit = 100): Promise<ReviewItem[]> {
  const data = await apiGet<{ items: ReviewItemApi[] }>(`/relevance/review?limit=${limit}`)
  return data.items.map((it) => ({
    inboxId: it.inbox_id,
    occurredAt: it.occurred_at,
    fromEmail: it.from_email,
    subject: it.subject,
    snippet: it.snippet,
    reason: it.reason,
    createdAt: it.created_at,
  }))
}

/** Resuelve un `insufficient` (mark + veredicto manual) — POST /relevance/review/{id}/resolve. */
export async function resolveReview(
  inboxId: number,
  isRelevant: boolean,
  reason: string | null = null,
): Promise<void> {
  await apiPost(`/relevance/review/${inboxId}/resolve`, { is_relevant: isRelevant, reason })
}

// ---- Lazo de sugerencia de intereses (rechazo manual → editar intereses) -----------------------

export type SuggestionAction = "add" | "remove"
export type SuggestionStatus = "proposed" | "accepted" | "rejected"

/** Sugerencia de editar la lista de intereses (segundo lazo: rechazo manual → intereses). Espeja
 *  la auditoría de la minería de reglas: se propone, el dueño la acepta (aplica el alta/baja) o la
 *  descarta. `add` crea un interés; `remove` pausa el interés `interestId`. */
export interface InterestSuggestion {
  id: number
  action: SuggestionAction
  text: string
  interestId: number | null
  rationale: string
  status: SuggestionStatus
  model: string | null
  createdAt: string
  resolvedAt: string | null
}

interface InterestSuggestionApi {
  id: number
  action: SuggestionAction
  text: string
  interest_id: number | null
  rationale: string
  status: string
  model: string | null
  created_at: string
  resolved_at: string | null
}

function toSuggestion(it: InterestSuggestionApi): InterestSuggestion {
  return {
    id: it.id,
    action: it.action,
    text: it.text,
    interestId: it.interest_id,
    rationale: it.rationale,
    status: it.status as SuggestionStatus,
    model: it.model,
    createdAt: it.created_at,
    resolvedAt: it.resolved_at,
  }
}

/** Sugerencias de editar intereses (pendientes por default) — GET /relevance/interests/suggestions. */
export async function fetchInterestSuggestions(status = "proposed"): Promise<InterestSuggestion[]> {
  const data = await apiGet<{ items: InterestSuggestionApi[] }>(
    `/relevance/interests/suggestions?status=${status}`,
  )
  return data.items.map(toSuggestion)
}

export interface MineInterestsResult {
  marks: number
  proposed: number
  inserted: number
  costUsd: number
}

/** Minería on-demand de sugerencias de interés (1 llamada LLM sobre las marcas) — POST /relevance/interests/mine. */
export async function mineInterests(): Promise<MineInterestsResult> {
  const r = await apiPost<{ marks: number; proposed: number; inserted: number; cost_usd: number }>(
    "/relevance/interests/mine",
  )
  return { marks: r.marks, proposed: r.proposed, inserted: r.inserted, costUsd: r.cost_usd }
}

/** Acepta (aplica el alta/baja) o descarta una sugerencia — POST /relevance/interests/suggestions/{id}/resolve. */
export async function resolveInterestSuggestion(
  id: number,
  accept: boolean,
): Promise<InterestSuggestion> {
  return toSuggestion(
    await apiPost<InterestSuggestionApi>(`/relevance/interests/suggestions/${id}/resolve`, {
      accept,
    }),
  )
}
