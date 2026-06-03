// Superficie del dominio IDENTIDADES contra la API real (router /identidades). Como `calendar.ts`/
// `finance.ts`: funciones async + transforms snake_case → camelCase. La vista `/directorio` consume
// el directorio de personas (Google Contacts), la lista de interés (orgs, con CRUD), las menciones
// extraídas con su resolución, y el estado de sync (cuentas + corridas + trigger).

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

// ---- Tipos del dominio (camelCase) ------------------------------------------------------------

export type IdentityKind = "organizacion" | "producto" | "agente"

export interface IdentityPerson {
  id: number
  displayName: string
  givenName: string | null
  familyName: string | null
  emails: string[]
  phones: string[]
  orgName: string | null
  role: string | null
  source: string
  interest: boolean
  provider: string | null
  photoUrl: string | null
  deleted: boolean
  createdAt: string
  updatedAt: string
}

export interface IdentityOrg {
  id: number
  name: string
  kind: IdentityKind
  aliases: string[]
  domains: string[]
  interest: boolean
  description: string
  source: string
  createdAt: string
  updatedAt: string
}

export interface IdentityMention {
  id: number
  sourceInboxIds: number[]
  evidence: string
  mentionedName: string
  mentionedKind: string
  email: string | null
  handle: string | null
  orgHint: string | null
  roleHint: string | null
  confidence: number | null
  resolvedKind: "person" | "org" | null
  resolvedPersonId: number | null
  resolvedOrgId: number | null
  resolutionMethod: string | null
  createdAt: string
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

export interface IdentityOrgInput {
  name: string
  kind: IdentityKind
  aliases: string[]
  domains: string[]
  description?: string
  interest?: boolean
}

// ---- Shapes crudos del API (snake_case) -------------------------------------------------------

interface PersonApi {
  id: number
  display_name: string
  given_name: string | null
  family_name: string | null
  emails: string[]
  phones: string[]
  org_name: string | null
  role: string | null
  source: string
  interest: boolean
  provider: string | null
  photo_url: string | null
  deleted: boolean
  created_at: string
  updated_at: string
}

interface OrgApi {
  id: number
  name: string
  kind: IdentityKind
  aliases: string[]
  domains: string[]
  interest: boolean
  description: string
  source: string
  created_at: string
  updated_at: string
}

interface MentionApi {
  id: number
  source_inbox_ids: number[]
  evidence: string
  mentioned_name: string
  mentioned_kind: string
  email: string | null
  handle: string | null
  org_hint: string | null
  role_hint: string | null
  confidence: number | null
  resolved_kind: "person" | "org" | null
  resolved_person_id: number | null
  resolved_org_id: number | null
  resolution_method: string | null
  created_at: string
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

interface PersonDetailApi {
  person: PersonApi
  orgs: OrgApi[]
  mentions: MentionApi[]
}

interface OrgDetailApi {
  org: OrgApi
  members: PersonApi[]
  mentions: MentionApi[]
}

// ---- Transforms -------------------------------------------------------------------------------

function toPerson(p: PersonApi): IdentityPerson {
  return {
    id: p.id,
    displayName: p.display_name,
    givenName: p.given_name,
    familyName: p.family_name,
    emails: p.emails,
    phones: p.phones,
    orgName: p.org_name,
    role: p.role,
    source: p.source,
    interest: p.interest,
    provider: p.provider,
    photoUrl: p.photo_url,
    deleted: p.deleted,
    createdAt: p.created_at,
    updatedAt: p.updated_at,
  }
}

function toOrg(o: OrgApi): IdentityOrg {
  return {
    id: o.id,
    name: o.name,
    kind: o.kind,
    aliases: o.aliases,
    domains: o.domains,
    interest: o.interest,
    description: o.description,
    source: o.source,
    createdAt: o.created_at,
    updatedAt: o.updated_at,
  }
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
    orgHint: m.org_hint,
    roleHint: m.role_hint,
    confidence: m.confidence,
    resolvedKind: m.resolved_kind,
    resolvedPersonId: m.resolved_person_id,
    resolvedOrgId: m.resolved_org_id,
    resolutionMethod: m.resolution_method,
    createdAt: m.created_at,
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

export async function fetchIdentityPersons(q?: string): Promise<IdentityPerson[]> {
  const query = q ? `?q=${encodeURIComponent(q)}` : ""
  const page = await apiGet<ListApi<PersonApi>>(`/identidades/persons${query}`)
  return page.items.map(toPerson)
}

export interface IdentityPersonDetail {
  person: IdentityPerson
  orgs: IdentityOrg[]
  mentions: IdentityMention[]
}

export async function fetchIdentityPerson(id: number): Promise<IdentityPersonDetail> {
  const d = await apiGet<PersonDetailApi>(`/identidades/persons/${id}`)
  return { person: toPerson(d.person), orgs: d.orgs.map(toOrg), mentions: d.mentions.map(toMention) }
}

export async function fetchIdentityOrgs(opts?: {
  q?: string
  interest?: boolean
}): Promise<IdentityOrg[]> {
  const params = new URLSearchParams()
  if (opts?.q) params.set("q", opts.q)
  if (opts?.interest !== undefined) params.set("interest", String(opts.interest))
  const qs = params.toString()
  const page = await apiGet<ListApi<OrgApi>>(`/identidades/orgs${qs ? `?${qs}` : ""}`)
  return page.items.map(toOrg)
}

export interface IdentityOrgDetail {
  org: IdentityOrg
  members: IdentityPerson[]
  mentions: IdentityMention[]
}

export async function fetchIdentityOrg(id: number): Promise<IdentityOrgDetail> {
  const d = await apiGet<OrgDetailApi>(`/identidades/orgs/${id}`)
  return { org: toOrg(d.org), members: d.members.map(toPerson), mentions: d.mentions.map(toMention) }
}

export async function fetchIdentityMentions(resolved?: boolean): Promise<IdentityMention[]> {
  const query = resolved !== undefined ? `?resolved=${String(resolved)}` : ""
  const page = await apiGet<ListApi<MentionApi>>(`/identidades/mentions${query}`)
  return page.items.map(toMention)
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

export async function createIdentityOrg(body: IdentityOrgInput): Promise<IdentityOrg> {
  return toOrg(await apiPost<OrgApi>("/identidades/orgs", body))
}

export async function updateIdentityOrg(
  id: number,
  body: Partial<IdentityOrgInput>,
): Promise<IdentityOrg> {
  return toOrg(await apiPatch<OrgApi>(`/identidades/orgs/${id}`, body))
}

export async function deleteIdentityOrg(id: number): Promise<void> {
  await apiDelete<{ deleted: boolean }>(`/identidades/orgs/${id}`)
}

export async function updateIdentityPerson(
  id: number,
  body: { interest?: boolean; display_name?: string; role?: string },
): Promise<IdentityPerson> {
  return toPerson(await apiPatch<PersonApi>(`/identidades/persons/${id}`, body))
}

export async function triggerIdentitySync(
  accountId: number,
  full = false,
): Promise<IdentitySyncResult> {
  return apiPost<IdentitySyncResult>("/identidades/sync", { account_id: accountId, full })
}

// ---- Detectadas (no-interés): lo que el sistema encontró y está por promover ------------------

export interface DetectedEntry {
  kind: "person" | "org"
  id: number
  name: string
  sub: string
}

export async function fetchDetected(): Promise<DetectedEntry[]> {
  const [persons, orgs] = await Promise.all([
    apiGet<ListApi<PersonApi>>("/identidades/persons?interest=false"),
    apiGet<ListApi<OrgApi>>("/identidades/orgs?interest=false"),
  ])
  return [
    ...persons.items.map((p) => ({
      kind: "person" as const,
      id: p.id,
      name: p.display_name,
      sub: p.emails[0] ?? p.org_name ?? "persona",
    })),
    ...orgs.items.map((o) => ({ kind: "org" as const, id: o.id, name: o.name, sub: o.kind })),
  ]
}
