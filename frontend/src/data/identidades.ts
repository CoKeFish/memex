// Superficie del dominio IDENTIDADES (modelo unificado) contra la API real (router /identidades).
// Como `calendar.ts`/`finance.ts`: funciones async + transforms snake_case → camelCase. La vista
// `/directorio` consume el directorio unificado (personas + organizaciones), el detalle de una
// identidad (identificadores por-fuente, sedes, afiliaciones, menciones), la cola de candidatos de
// merge (zona gris del difuso) y el estado de sync de contactos.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

// ---- Tipos del dominio (camelCase) ------------------------------------------------------------

export type IdentityKind = "persona" | "organizacion"

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
  createdAt: string
  updatedAt: string
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
  created_at: string
  updated_at: string
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
    createdAt: i.created_at,
    updatedAt: i.updated_at,
  }
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
  return toIdentity(await apiPatch<IdentityApi>(`/identidades/${id}`, body))
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
