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

// ----- Telegram: login multi-paso + discover/selección de chats ----- //

export interface TelegramCodeStart {
  /** token opaco que correlaciona request-code ↔ submit-code (vacío si ya estaba autorizado). */
  state: string
  phoneMasked: string
  alreadyAuthorized: boolean
}

/** Paso 1: manda el código al teléfono de la cuenta (credenciales del vault, server-side). */
export async function requestTelegramCode(accountId: number): Promise<TelegramCodeStart> {
  const r = await apiPost<{ status: string; state?: string; phone_masked: string }>(
    `/accounts/${accountId}/telegram/request-code`,
  )
  return {
    state: r.state ?? "",
    phoneMasked: r.phone_masked,
    alreadyAuthorized: r.status === "already_authorized",
  }
}

/** Paso 2: envía el código. Devuelve "ok" o "2fa_required" (hay verificación en dos pasos). */
export async function submitTelegramCode(
  accountId: number,
  state: string,
  code: string,
): Promise<"ok" | "2fa_required"> {
  const r = await apiPost<{ status: string }>(`/accounts/${accountId}/telegram/submit-code`, {
    state,
    code,
  })
  return r.status === "2fa_required" ? "2fa_required" : "ok"
}

/** Paso 2b (2FA): envía la contraseña de verificación en dos pasos. */
export async function submitTelegramPassword(
  accountId: number,
  state: string,
  password: string,
): Promise<void> {
  await apiPost<{ status: string }>(`/accounts/${accountId}/telegram/submit-password`, {
    state,
    password,
  })
}

export interface TelegramChat {
  chatId: number
  name: string
  kind: "channel" | "group" | "user"
}

/** Discover: lista los grupos/canales accesibles (requiere sesión = login hecho). */
export async function discoverTelegramChats(accountId: number): Promise<TelegramChat[]> {
  const r = await apiGet<{ chats: { chat_id: number; name: string; kind: string }[] }>(
    `/accounts/${accountId}/telegram/chats`,
  )
  return r.chats.map((c) => ({
    chatId: c.chat_id,
    name: c.name,
    kind: c.kind as TelegramChat["kind"],
  }))
}

export interface TelegramSourceInfo {
  sourceId: number
  config: Record<string, unknown>
  allowedChatIds: number[]
}

/** La source telegram vinculada a la cuenta (para leer/guardar allowed_chats). null si no hay. */
export async function getTelegramSource(accountId: number): Promise<TelegramSourceInfo | null> {
  const rows = await apiGet<
    { id: number; type: string; account_id: number | null; config: Record<string, unknown> }[]
  >("/sources")
  const src = rows.find((s) => s.type === "telegram" && s.account_id === accountId)
  if (!src) return null
  const raw = (src.config?.["allowed_chats"] as { chat_id?: unknown }[] | undefined) ?? []
  const allowedChatIds = raw
    .map((c) => Number(c.chat_id))
    .filter((n) => Number.isFinite(n))
  return { sourceId: src.id, config: src.config ?? {}, allowedChatIds }
}

export interface AllowedChatInput {
  chatId: number
  streaming?: boolean
  priority?: boolean
}

/** Persiste allowed_chats en el config del source (PATCH /sources/{id}; preserva el resto). */
export async function setAllowedChats(
  sourceId: number,
  chats: AllowedChatInput[],
  baseConfig: Record<string, unknown> = {},
): Promise<void> {
  const allowed_chats = chats.map((c) => ({
    chat_id: c.chatId,
    streaming: c.streaming ?? false,
    priority: c.priority ?? false,
  }))
  await apiPatch<unknown>(`/sources/${sourceId}`, { config: { ...baseConfig, allowed_chats } })
}
