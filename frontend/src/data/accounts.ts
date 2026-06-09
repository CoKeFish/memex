// Gestión de cuentas y credenciales contra la API real (vault cifrado server-side).
// El API nunca devuelve el plaintext de una credencial: solo `configured` + `last4`.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

export type AccountKind = "email" | "chat" | "social"
export type HealthStatus = "unknown" | "healthy" | "degraded" | "unhealthy"

export interface CredentialStatus {
  secretName: string
  configured: boolean
  last4: string
  /** "vault" (cifrada en DB) · "env" (variable de entorno del contenedor) · "" si falta. */
  source: "vault" | "env" | ""
}

export interface ManagedAccount {
  id: number
  alias: string
  provider: string
  kind: AccountKind
  enabled: boolean
  healthStatus: HealthStatus
  lastHealthCheckAt: string | null
  metadata: Record<string, unknown>
  secrets: CredentialStatus[]
  createdAt: string
}

interface CredentialApi {
  secret_name: string
  configured: boolean
  last4: string
  source?: string
}

interface AccountApi {
  id: number
  user_id: number
  alias: string
  provider: string
  kind: string
  metadata: Record<string, unknown>
  enabled: boolean
  health_status: string
  last_health_check_at: string | null
  created_at: string
  secrets: CredentialApi[]
}

function toCredential(c: CredentialApi): CredentialStatus {
  return {
    secretName: c.secret_name,
    configured: c.configured,
    last4: c.last4,
    source: (c.source ?? "") as CredentialStatus["source"],
  }
}

function toAccount(a: AccountApi): ManagedAccount {
  return {
    id: a.id,
    alias: a.alias,
    provider: a.provider,
    kind: a.kind as AccountKind,
    enabled: a.enabled,
    healthStatus: a.health_status as HealthStatus,
    lastHealthCheckAt: a.last_health_check_at,
    metadata: a.metadata,
    secrets: a.secrets.map(toCredential),
    createdAt: a.created_at,
  }
}

/** Nombres de credencial esperados por proveedor (espeja el resolver del backend). */
export const SECRET_NAMES: Record<string, string[]> = {
  imap: ["username", "password"],
  telegram: ["api_id", "api_hash", "phone"],
  instagram: ["apify_token"],
  facebook: ["apify_token"],
  x: ["apify_token"],
}

export async function fetchAccounts(): Promise<ManagedAccount[]> {
  const rows = await apiGet<AccountApi[]>("/accounts")
  return rows.map(toAccount)
}

export async function createAccount(input: {
  alias: string
  provider: string
  kind: AccountKind
  metadata?: Record<string, unknown>
}): Promise<ManagedAccount> {
  return toAccount(
    await apiPost<AccountApi>("/accounts", {
      alias: input.alias,
      provider: input.provider,
      kind: input.kind,
      metadata: input.metadata ?? {},
    }),
  )
}

export async function patchAccount(
  id: number,
  patch: { alias?: string; enabled?: boolean; metadata?: Record<string, unknown> },
): Promise<ManagedAccount> {
  return toAccount(await apiPatch<AccountApi>(`/accounts/${id}`, patch))
}

export async function deleteAccount(id: number, opts?: { cascade?: boolean }): Promise<void> {
  const q = opts?.cascade ? "?cascade=true" : ""
  await apiDelete<void>(`/accounts/${id}${q}`)
}

export async function setCredential(
  accountId: number,
  secretName: string,
  value: string,
): Promise<CredentialStatus> {
  return toCredential(
    await apiPost<CredentialApi>(`/accounts/${accountId}/credentials`, {
      secret_name: secretName,
      value,
    }),
  )
}

export async function deleteCredential(accountId: number, secretName: string): Promise<void> {
  await apiDelete<void>(`/accounts/${accountId}/credentials/${secretName}`)
}

export interface HealthCheckResult {
  status: HealthStatus
  detail: string
  checkedAt: string
}

export async function healthCheckAccount(accountId: number): Promise<HealthCheckResult> {
  const r = await apiPost<{ status: string; detail: string; checked_at: string }>(
    `/accounts/${accountId}/health-check`,
  )
  return { status: r.status as HealthStatus, detail: r.detail, checkedAt: r.checked_at }
}

/** Vincula (o desvincula con accountId=null) una source a una cuenta (PATCH /sources/{id}). */
export async function linkSourceToAccount(
  sourceId: number,
  accountId: number | null,
): Promise<void> {
  await apiPatch<unknown>(`/sources/${sourceId}`, { account_id: accountId })
}

/** Nombre del secreto del token OAuth de Google en el vault (espeja el backend). */
export const GOOGLE_OAUTH_SECRET = "google_oauth_token"

/** Inicia el flujo "Conectar con Google": devuelve la URL de consentimiento para redirigir. */
export async function startGoogleOAuth(accountId: number): Promise<string> {
  const r = await apiGet<{ authorization_url: string }>(
    `/accounts/${accountId}/oauth/google/start`,
  )
  return r.authorization_url
}
