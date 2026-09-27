// Superficie del dominio IDENTIDADES (modelo unificado) contra la API real (router /identidades).
// Como `calendar.ts`/`finance.ts`: funciones async + transforms snake_case → camelCase. La vista
// `/directorio` consume el directorio unificado (personas + organizaciones + productos), el detalle
// de una identidad (identificadores por-fuente, sedes, afiliaciones, menciones), la cola de
// candidatos de merge (zona gris del difuso) y el estado de sync de contactos.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

// ---- Tipos del dominio (camelCase) ------------------------------------------------------------

export type IdentityKind = "persona" | "organizacion" | "producto"

export interface Identity {
  id: number
  kind: IdentityKind
  displayName: string
  aliases: string[]
  interest: boolean
  source: string
  notes: string
  givenName: string | null
  familyName: string | null
  birthday: string | null
  photoUrl: string | null
  deleted: boolean
  // Pertenencia («sub»): de qué identidad cuelga esta. mentionCount = nº de menciones resueltas acá.
  parentId: number | null
  parentName: string | null
  mentionCount: number
  createdAt: string
  updatedAt: string
}

export interface IdentityChild {
  id: number
  kind: IdentityKind
  displayName: string
}

export interface IdentityIdentifier {
  id: number
  platform: string
  kind: "email" | "phone" | "handle" | "domain" | "url"
  value: string
  isPrimary: boolean
  source: string
}

export interface IdentitySite {
  id: number
  label: string
  address: string
  country: string | null
}

export interface IdentityAffiliation {
  id: number
  kind: IdentityKind
  displayName: string
  role: string | null
}

export interface IdentityMention {
  id: number
  sourceInboxIds: number[]
  evidence: string
  mentionedName: string
  mentionedKind: string
  email: string | null
  handle: string | null
  confidence: number | null
  resolvedKind: IdentityKind | null
  resolvedIdentityId: number | null
  resolutionMethod: string | null
  createdAt: string
}

export interface IdentityDetail {
  identity: Identity
  identifiers: IdentityIdentifier[]
  sites: IdentitySite[]
  affiliations: IdentityAffiliation[]
  mentions: IdentityMention[]
  children: IdentityChild[]
}

export interface IdentityMergeCandidate {
  id: number
  identityAId: number
  identityBId: number
  aName: string
  bName: string
  kind: IdentityKind
  reason: string
  score: number | null
  status: string
}

export interface IdentityProviderAccount {
  id: number
  provider: string
  accountLabel: string
  accountId: number | null
  enabled: boolean
  lastSyncAt: string | null
  syncTokenPresent: boolean
}

export interface IdentitySyncRun {
  id: number
  providerAccountId: number | null
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  errors: number
  status: string
  startedAt: string
  finishedAt: string | null
}

export interface IdentitySyncResult {
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  errors: number
}

export interface IdentityCreateInput {
  kind: IdentityKind
  displayName: string
  aliases?: string[]
  interest?: boolean
  notes?: string
}

export interface IdentityUpdateInput {
  displayName?: string
  kind?: IdentityKind
  interest?: boolean
  notes?: string
  birthday?: string | null
  aliases?: string[]
  // null = quitar el padre; number = setearlo; undefined = no tocar.
  parentId?: number | null
}

// ---- Shapes crudos del API (snake_case) -------------------------------------------------------

interface IdentityApi {
  id: number
  kind: IdentityKind
  display_name: string
  aliases: string[]
  interest: boolean
  source: string
  notes: string
  given_name: string | null
  family_name: string | null
  birthday: string | null
  photo_url: string | null
  deleted: boolean
  parent_id: number | null
  parent_name: string | null
  mention_count: number
  created_at: string
  updated_at: string
}

interface ChildApi {
  id: number
  kind: IdentityKind
  display_name: string
}

interface IdentifierApi {
  id: number
  platform: string
  kind: IdentityIdentifier["kind"]
  value: string
  is_primary: boolean
  source: string
}

interface SiteApi {
  id: number
  label: string
  address: string
  country: string | null
}

interface AffiliationApi {
  id: number
  kind: IdentityKind
  display_name: string
  role: string | null
}

interface MentionApi {
  id: number
  source_inbox_ids: number[]
  evidence: string
  mentioned_name: string
  mentioned_kind: string
  email: string | null
  handle: string | null
  confidence: number | null
  resolved_kind: IdentityKind | null
  resolved_identity_id: number | null
  resolution_method: string | null
  created_at: string
}

interface DetailApi {
  identity: IdentityApi
  identifiers: IdentifierApi[]
  sites: SiteApi[]
  affiliations: AffiliationApi[]
  mentions: MentionApi[]
  children: ChildApi[]
}

interface MergeCandidateApi {
  id: number
  identity_a_id: number
  identity_b_id: number
  a_name: string
  b_name: string
  kind: IdentityKind
  reason: string
  score: number | null
  status: string
}

interface ProviderAccountApi {
  id: number
  provider: string
  account_label: string
  account_id: number | null
  enabled: boolean
  last_sync_at: string | null
  sync_token_present: boolean
}

interface SyncRunApi {
  id: number
  provider_account_id: number | null
  pulled: number
  created: number
  modified: number
  deleted: number
  unchanged: number
  errors: number
  status: string
  started_at: string
  finished_at: string | null
}

interface ListApi<T> {
  items: T[]
  next_cursor: number | null
}

// ---- Transforms -------------------------------------------------------------------------------

function toIdentity(i: IdentityApi): Identity {
  return {
    id: i.id,
    kind: i.kind,
    displayName: i.display_name,
    aliases: i.aliases,
    interest: i.interest,
    source: i.source,
    notes: i.notes,
    givenName: i.given_name,
    familyName: i.family_name,
    birthday: i.birthday,
    photoUrl: i.photo_url,
    deleted: i.deleted,
    parentId: i.parent_id,
    parentName: i.parent_name,
    mentionCount: i.mention_count,
    createdAt: i.created_at,
    updatedAt: i.updated_at,
  }
}

function toChild(c: ChildApi): IdentityChild {
  return { id: c.id, kind: c.kind, displayName: c.display_name }
}

function toIdentifier(i: IdentifierApi): IdentityIdentifier {
  return {
    id: i.id,
    platform: i.platform,
    kind: i.kind,
    value: i.value,
    isPrimary: i.is_primary,
    source: i.source,
  }
}

function toSite(s: SiteApi): IdentitySite {
  return { id: s.id, label: s.label, address: s.address, country: s.country }
}

function toAffiliation(a: AffiliationApi): IdentityAffiliation {
  return { id: a.id, kind: a.kind, displayName: a.display_name, role: a.role }
}

function toMention(m: MentionApi): IdentityMention {
  return {
    id: m.id,
    sourceInboxIds: m.source_inbox_ids,
    evidence: m.evidence,
    mentionedName: m.mentioned_name,
    mentionedKind: m.mentioned_kind,
    email: m.email,
    handle: m.handle,
    confidence: m.confidence,
    resolvedKind: m.resolved_kind,
    resolvedIdentityId: m.resolved_identity_id,
    resolutionMethod: m.resolution_method,
    createdAt: m.created_at,
  }
}

function toMergeCandidate(c: MergeCandidateApi): IdentityMergeCandidate {
  return {
    id: c.id,
    identityAId: c.identity_a_id,
    identityBId: c.identity_b_id,
    aName: c.a_name,
    bName: c.b_name,
    kind: c.kind,
    reason: c.reason,
    score: c.score,
    status: c.status,
  }
}

function toAccount(a: ProviderAccountApi): IdentityProviderAccount {
  return {
    id: a.id,
    provider: a.provider,
    accountLabel: a.account_label,
    accountId: a.account_id,
    enabled: a.enabled,
    lastSyncAt: a.last_sync_at,
    syncTokenPresent: a.sync_token_present,
  }
}

function toRun(r: SyncRunApi): IdentitySyncRun {
  return {
    id: r.id,
    providerAccountId: r.provider_account_id,
    pulled: r.pulled,
    created: r.created,
    modified: r.modified,
    deleted: r.deleted,
    unchanged: r.unchanged,
    errors: r.errors,
    status: r.status,
    startedAt: r.started_at,
    finishedAt: r.finished_at,
  }
}

// ---- Fetchers ---------------------------------------------------------------------------------

export async function fetchIdentities(opts?: {
  q?: string
  kind?: IdentityKind
  interest?: boolean
}): Promise<Identity[]> {
  const params = new URLSearchParams()
  if (opts?.q) params.set("q", opts.q)
  if (opts?.kind) params.set("kind", opts.kind)
  if (opts?.interest !== undefined) params.set("interest", String(opts.interest))
  const qs = params.toString()
  const page = await apiGet<ListApi<IdentityApi>>(`/identidades${qs ? `?${qs}` : ""}`)
  return page.items.map(toIdentity)
}

export async function fetchIdentity(id: number): Promise<IdentityDetail> {
  const d = await apiGet<DetailApi>(`/identidades/${id}`)
  return {
    identity: toIdentity(d.identity),
    identifiers: d.identifiers.map(toIdentifier),
    sites: d.sites.map(toSite),
    affiliations: d.affiliations.map(toAffiliation),
    mentions: d.mentions.map(toMention),
    children: d.children.map(toChild),
  }
}

export async function fetchIdentityMentions(resolved?: boolean): Promise<IdentityMention[]> {
  const query = resolved !== undefined ? `?resolved=${String(resolved)}` : ""
  const page = await apiGet<ListApi<MentionApi>>(`/identidades/mentions${query}`)
  return page.items.map(toMention)
}

export async function fetchMergeCandidates(): Promise<IdentityMergeCandidate[]> {
  const page = await apiGet<{ items: MergeCandidateApi[] }>("/identidades/merge-candidates")
  return page.items.map(toMergeCandidate)
}

export async function fetchIdentityProviderAccounts(): Promise<IdentityProviderAccount[]> {
  const page = await apiGet<{ items: ProviderAccountApi[] }>("/identidades/provider-accounts")
  return page.items.map(toAccount)
}

export async function fetchIdentitySyncRuns(): Promise<IdentitySyncRun[]> {
  const page = await apiGet<ListApi<SyncRunApi>>("/identidades/sync-runs")
  return page.items.map(toRun)
}

// ---- Mutations --------------------------------------------------------------------------------

export async function createIdentity(input: IdentityCreateInput): Promise<Identity> {
  return toIdentity(
    await apiPost<IdentityApi>("/identidades", {
      kind: input.kind,
      display_name: input.displayName,
      aliases: input.aliases ?? [],
      interest: input.interest ?? true,
      notes: input.notes ?? "",
    }),
  )
}

export async function updateIdentity(id: number, input: IdentityUpdateInput): Promise<Identity> {
  const body: Record<string, unknown> = {}
  if (input.displayName !== undefined) body.display_name = input.displayName
  if (input.kind !== undefined) body.kind = input.kind
  if (input.interest !== undefined) body.interest = input.interest
  if (input.notes !== undefined) body.notes = input.notes
  if (input.birthday !== undefined) body.birthday = input.birthday
  if (input.aliases !== undefined) body.aliases = input.aliases
  if (input.parentId !== undefined) body.parent_id = input.parentId
  return toIdentity(await apiPatch<IdentityApi>(`/identidades/${id}`, body))
}

export interface OrganizeResult {
  orgs: number
  linked: number
  created: number
  cleaned: number
  skipped: number
}

/** Organiza la jerarquía de pertenencia con el LLM (sin confirmación manual). */
export async function organizeHierarchy(): Promise<OrganizeResult> {
  return apiPost<OrganizeResult>("/identidades/organize", {})
}

export async function deleteIdentity(id: number): Promise<void> {
  await apiDelete<{ deleted: boolean }>(`/identidades/${id}`)
}

export async function addIdentifier(
  identityId: number,
  input: { platform: string; kind: IdentityIdentifier["kind"]; value: string; isPrimary?: boolean },
): Promise<IdentityIdentifier> {
  return toIdentifier(
    await apiPost<IdentifierApi>(`/identidades/${identityId}/identifiers`, {
      platform: input.platform,
      kind: input.kind,
      value: input.value,
      is_primary: input.isPrimary ?? false,
    }),
  )
}

export async function deleteIdentifier(identityId: number, identifierId: number): Promise<void> {
  await apiDelete<{ deleted: boolean }>(`/identidades/${identityId}/identifiers/${identifierId}`)
}

export async function addSite(
  identityId: number,
  input: { label?: string; address?: string; country?: string | null },
): Promise<IdentitySite> {
  return toSite(
    await apiPost<SiteApi>(`/identidades/${identityId}/sites`, {
      label: input.label ?? "",
      address: input.address ?? "",
      country: input.country ?? null,
    }),
  )
}

export async function deleteSite(identityId: number, siteId: number): Promise<void> {
  await apiDelete<{ deleted: boolean }>(`/identidades/${identityId}/sites/${siteId}`)
}

export async function affiliate(
  personId: number,
  orgId: number,
  role?: string,
): Promise<IdentityDetail> {
  const d = await apiPost<DetailApi>(`/identidades/${personId}/orgs`, {
    org_id: orgId,
    role: role ?? null,
  })
  return {
    identity: toIdentity(d.identity),
    identifiers: d.identifiers.map(toIdentifier),
    sites: d.sites.map(toSite),
    affiliations: d.affiliations.map(toAffiliation),
    mentions: d.mentions.map(toMention),
    children: d.children.map(toChild),
  }
}

export async function mergeIdentities(survivorId: number, absorbedId: number): Promise<Identity> {
  return toIdentity(
    await apiPost<IdentityApi>("/identidades/merge", {
      survivor_id: survivorId,
      absorbed_id: absorbedId,
    }),
  )
}

export async function confirmMergeCandidate(candidateId: number): Promise<Identity> {
  return toIdentity(
    await apiPost<IdentityApi>(`/identidades/merge-candidates/${candidateId}/confirm`, {}),
  )
}

export async function rejectMergeCandidate(candidateId: number): Promise<void> {
  await apiPost<{ rejected: boolean }>(`/identidades/merge-candidates/${candidateId}/reject`, {})
}

export async function triggerIdentitySync(
  accountId: number,
  full = false,
): Promise<IdentitySyncResult> {
  return apiPost<IdentitySyncResult>("/identidades/sync", { account_id: accountId, full })
}

// ---- Jerarquía evaluada contra el libro de evidencia (vista /jerarquia) -------------------------

export interface HierarchyCandidate {
  targetId: number
  targetName: string
  messages: number
  support: number
  bestConfidence: number
  methods: string[]
  pinned: boolean
}

export interface HierarchyNode {
  id: number
  kind: IdentityKind
  displayName: string
  parentId: number | null
  parentName: string | null
  parentSource: string | null
  pinned: boolean
  messages: number
  support: number
  bestConfidence: number
  methods: string[]
  disputed: boolean
  weak: boolean
  unresolvedNames: string[]
  candidates: HierarchyCandidate[]
  childrenCount: number
  mentionCount: number
  createdBy: string | null
}

export interface HierarchyAffiliation {
  personId: number
  personName: string
  orgId: number
  orgName: string
  role: string
  messages: number
  roles: string[]
}

export interface HierarchyData {
  nodes: HierarchyNode[]
  affiliations: HierarchyAffiliation[]
  settings: { parentMinMessages: number; parentSingleMinConfidence: number; minConfidenceParent: number }
}

export interface LinkClaim {
  id: number
  kind: "pertenece_a" | "afiliado"
  sourceId: number
  sourceName: string | null
  targetId: number | null
  targetName: string | null
  asWritten: string | null
  role: string | null
  confidence: number
  method: string
  inboxId: number | null
  subject: string | null
  occurredAt: string | null
  status: "active" | "rejected"
  rationale: string | null
  createdAt: string
}

export interface LinkClaims {
  outgoing: LinkClaim[]
  incoming: LinkClaim[]
}

export interface CurateInput {
  scope: "disputed" | "weak" | "unresolved" | "candidates" | "roots" | "all" | "tree"
  limit?: number
  dryRun?: boolean
  webSearch?: boolean
  rounds?: number
}

export interface CurateResult {
  items: number
  batches: number
  decisions: number
  kept: number
  set: number
  created: number
  cleared: number
  merged: number
  deleted: number
  skipped: number
  dropped: number
  errors: number
  llmCalls: number
  latencyMs: number
  dryRun: boolean
  applied: {
    item: string
    action: string
    result: string
    parentId?: number | null
    parentName?: string | null
    confidence?: number
    rationale?: string | null
  }[]
}

interface HierarchyCandidateApi {
  target_id: number
  target_name: string
  messages: number
  support: number
  best_confidence: number
  methods: string[]
  pinned: boolean
}

interface HierarchyNodeApi {
  id: number
  kind: IdentityKind
  display_name: string
  parent_id: number | null
  parent_name: string | null
  parent_source: string | null
  pinned: boolean
  messages: number
  support: number
  best_confidence: number
  methods: string[]
  disputed: boolean
  weak: boolean
  unresolved_names: string[]
  candidates: HierarchyCandidateApi[]
  children_count: number
  mention_count: number
  created_by: string | null
}

interface HierarchyAffiliationApi {
  person_id: number
  person_name: string
  org_id: number
  org_name: string
  role: string
  messages: number
  roles: string[]
}

interface HierarchyApi {
  nodes: HierarchyNodeApi[]
  affiliations: HierarchyAffiliationApi[]
  settings: {
    parent_min_messages: number
    parent_single_min_confidence: number
    min_confidence_parent: number
  }
}

interface LinkClaimApi {
  id: number
  kind: "pertenece_a" | "afiliado"
  source_id: number
  source_name: string | null
  target_id: number | null
  target_name: string | null
  as_written: string | null
  role: string | null
  confidence: number
  method: string
  inbox_id: number | null
  subject: string | null
  occurred_at: string | null
  status: "active" | "rejected"
  rationale: string | null
  created_at: string
}

interface CurateResultApi {
  items: number
  batches: number
  decisions: number
  kept: number
  set: number
  created: number
  cleared: number
  merged: number
  deleted: number
  skipped: number
  dropped: number
  errors: number
  llm_calls: number
  latency_ms: number
  dry_run: boolean
  applied: {
    item: string
    action: string
    result: string
    parent_id?: number | null
    parent_name?: string | null
    confidence?: number
    rationale?: string | null
  }[]
}

function toHierarchyNode(n: HierarchyNodeApi): HierarchyNode {
  return {
    id: n.id,
    kind: n.kind,
    displayName: n.display_name,
    parentId: n.parent_id,
    parentName: n.parent_name,
    parentSource: n.parent_source,
    pinned: n.pinned,
    messages: n.messages,
    support: n.support,
    bestConfidence: n.best_confidence,
    methods: n.methods,
    disputed: n.disputed,
    weak: n.weak,
    unresolvedNames: n.unresolved_names,
    candidates: n.candidates.map((c) => ({
      targetId: c.target_id,
      targetName: c.target_name,
      messages: c.messages,
      support: c.support,
      bestConfidence: c.best_confidence,
      methods: c.methods,
      pinned: c.pinned,
    })),
    childrenCount: n.children_count,
    mentionCount: n.mention_count,
    createdBy: n.created_by,
  }
}

function toClaim(c: LinkClaimApi): LinkClaim {
  return {
    id: c.id,
    kind: c.kind,
    sourceId: c.source_id,
    sourceName: c.source_name,
    targetId: c.target_id,
    targetName: c.target_name,
    asWritten: c.as_written,
    role: c.role,
    confidence: c.confidence,
    method: c.method,
    inboxId: c.inbox_id,
    subject: c.subject,
    occurredAt: c.occurred_at,
    status: c.status,
    rationale: c.rationale,
    createdAt: c.created_at,
  }
}

export async function fetchHierarchy(): Promise<HierarchyData> {
  const r = await apiGet<HierarchyApi>("/identidades/hierarchy")
  return {
    nodes: r.nodes.map(toHierarchyNode),
    affiliations: r.affiliations.map((a) => ({
      personId: a.person_id,
      personName: a.person_name,
      orgId: a.org_id,
      orgName: a.org_name,
      role: a.role,
      messages: a.messages,
      roles: a.roles,
    })),
    settings: {
      parentMinMessages: r.settings.parent_min_messages,
      parentSingleMinConfidence: r.settings.parent_single_min_confidence,
      minConfidenceParent: r.settings.min_confidence_parent,
    },
  }
}

export async function fetchIdentityClaims(
  id: number,
  includeRejected = false,
): Promise<LinkClaims> {
  const q = includeRejected ? "?include_rejected=true" : ""
  const r = await apiGet<{ outgoing: LinkClaimApi[]; incoming: LinkClaimApi[] }>(
    `/identidades/${id}/claims${q}`,
  )
  return { outgoing: r.outgoing.map(toClaim), incoming: r.incoming.map(toClaim) }
}

export async function rejectClaim(claimId: number, restore = false): Promise<LinkClaim> {
  const q = restore ? "?restore=true" : ""
  return toClaim(await apiPost<LinkClaimApi>(`/identidades/claims/${claimId}/reject${q}`, {}))
}

export async function confirmParent(id: number): Promise<Identity> {
  return toIdentity(await apiPost<IdentityApi>(`/identidades/${id}/confirm-parent`, {}))
}

export async function deriveHierarchy(
  identityId?: number,
): Promise<{ childId: number; parentId: number | null; reason: string }[]> {
  const q = identityId != null ? `?identity_id=${identityId}` : ""
  const r = await apiPost<{ changed: { child_id: number; parent_id: number | null; reason: string }[] }>(
    `/identidades/derive${q}`,
    {},
  )
  return r.changed.map((c) => ({ childId: c.child_id, parentId: c.parent_id, reason: c.reason }))
}

export async function seedClaims(): Promise<{
  parents: number
  affiliations: number
  mentionAffiliations: number
}> {
  const r = await apiPost<{ parents: number; affiliations: number; mention_affiliations: number }>(
    "/identidades/seed-claims",
    {},
  )
  return { parents: r.parents, affiliations: r.affiliations, mentionAffiliations: r.mention_affiliations }
}

export async function curateHierarchy(input: CurateInput): Promise<CurateResult> {
  const r = await apiPost<CurateResultApi>("/identidades/curate", {
    scope: input.scope,
    limit: input.limit ?? 40,
    dry_run: input.dryRun ?? false,
    web_search: input.webSearch ?? true,
    rounds: input.rounds ?? 1,
  })
  return {
    items: r.items,
    batches: r.batches,
    decisions: r.decisions,
    kept: r.kept,
    set: r.set,
    created: r.created,
    cleared: r.cleared,
    merged: r.merged,
    deleted: r.deleted,
    skipped: r.skipped,
    dropped: r.dropped,
    errors: r.errors,
    llmCalls: r.llm_calls,
    latencyMs: r.latency_ms,
    dryRun: r.dry_run,
    applied: r.applied.map((a) => ({
      item: a.item,
      action: a.action,
      result: a.result,
      parentId: a.parent_id,
      parentName: a.parent_name,
      confidence: a.confidence,
      rationale: a.rationale,
    })),
  }
}
