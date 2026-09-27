// Gestión de cuentas y credenciales contra la API real (vault cifrado server-side).
// El API nunca devuelve el plaintext de una credencial: solo `configured` + `last4`.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

export type AccountKind = "email" | "chat" | "social" | "notes"
export type HealthStatus = "unknown" | "healthy" | "degraded" | "unhealthy"

export interface CredentialStatus {
  secretName: string
  configured: boolean
  last4: string
  /** "vault" (cifrada en DB) · "env" (variable de entorno del contenedor) · "" si falta. */
  source: "vault" | "env" | ""
}

/** De dónde sale `healthStatus`: la validación (manual o auto-chequeo del connect) más reciente,
 * el último run de ingesta (si es más nuevo que la validación), o nada todavía. */
export type HealthBasis = "check" | "ingestion" | "none"

export interface ManagedAccount {
  id: number
  alias: string
  provider: string
  kind: AccountKind
  enabled: boolean
  healthStatus: HealthStatus
  lastHealthCheckAt: string | null
  /** Texto legible del último chequeo (persistido en el backend). null = nunca validada. */
  healthDetail: string | null
  /** Código estable de la causa; `reauth_required` = hay que reconectar con Google. */
  healthCode: string | null
  healthBasis: HealthBasis
  /** Instante de la evidencia que decide `healthStatus` (chequeo o ingesta). */
  healthEvidenceAt: string | null
  /** La evidencia venció (> 24 h) o no existe: el «OK» no se sostiene; se revalida. */
  healthStale: boolean
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
  health_detail?: string | null
  health_code?: string | null
  health_basis?: string
  health_evidence_at?: string | null
  health_stale?: boolean
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
    healthDetail: a.health_detail ?? null,
    healthCode: a.health_code ?? null,
    healthBasis: (a.health_basis ?? "none") as HealthBasis,
    healthEvidenceAt: a.health_evidence_at ?? null,
    healthStale: a.health_stale ?? false,
    metadata: a.metadata,
    secrets: a.secrets.map(toCredential),
    createdAt: a.created_at,
  }
}

/** Nombres de credencial que se cargan A MANO por proveedor (espeja el resolver del backend).
 * `google` = cuenta ya conectada por OAuth: el token NO se pega a mano (va por «Conectar»). */
export const SECRET_NAMES: Record<string, string[]> = {
  imap: ["username", "password"],
  google: ["username"],
  telegram: ["api_id", "api_hash", "phone"],
  whatsapp: [],
  instagram: ["apify_token"],
  facebook: ["apify_token"],
  x: ["apify_token"],
  notion: ["integration_token"],
}

/** Rótulo humano de cada credencial (la clave técnica queda como tooltip). */
export const SECRET_LABELS: Record<string, string> = {
  username: "usuario (email)",
  password: "contraseña de aplicación",
  google_oauth_token: "acceso OAuth de Google",
  api_id: "api_id de Telegram",
  api_hash: "api_hash de Telegram",
  phone: "teléfono",
  apify_token: "token de Apify",
  integration_token: "token de integración de Notion",
}

/** Rótulo humano del estado de salud (el valor crudo del API queda como tooltip). */
export const HEALTH_LABEL: Record<HealthStatus, string> = {
  healthy: "OK",
  degraded: "degradada",
  unhealthy: "con error",
  unknown: "sin validar",
}

/** `HealthResult.code` del backend: el permiso OAuth caducó/fue revocado → reconectar. */
export const HEALTH_CODE_REAUTH = "reauth_required"

/** Estado del vínculo con Google de una cuenta de email, para decidir QUÉ acción ofrecer:
 * - `not_connected`: nunca se conectó → «Conectar con Google».
 * - `connected`: hay token y la última evidencia es sana → badge + «Reconectar» discreto.
 * - `failing`: hay token pero la cuenta está con error por otra causa → detalle + «Reconectar».
 * - `reauth`: el backend dijo que el permiso caducó → «Reconectar con Google» destacado.
 * null = la cuenta no es de email (no aplica). */
/** `unverified`: hay token pero la evidencia de salud venció o no existe (no se pinta verde). */
export type GoogleLinkState = "not_connected" | "connected" | "unverified" | "failing" | "reauth"

export function googleLinkState(a: ManagedAccount): GoogleLinkState | null {
  if (a.kind !== "email") return null
  const hasToken = a.secrets.some((s) => s.secretName === GOOGLE_OAUTH_SECRET && s.configured)
  if (!hasToken) return "not_connected"
  if (a.healthCode === HEALTH_CODE_REAUTH) return "reauth"
  if (a.healthStatus === "unhealthy") return "failing"
  if (a.healthStale || a.healthStatus !== "healthy") return "unverified"
  return "connected"
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
  code: string | null
}

export async function healthCheckAccount(accountId: number): Promise<HealthCheckResult> {
  const r = await apiPost<{ status: string; detail: string; checked_at: string; code?: string | null }>(
    `/accounts/${accountId}/health-check`,
  )
  return {
    status: r.status as HealthStatus,
    detail: r.detail,
    checkedAt: r.checked_at,
    code: r.code ?? null,
  }
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
  /** supergrupo con TEMAS (forum) — tiene sub-canales seleccionables (topics). */
  isForum: boolean
}

/** Discover: lista los grupos/canales accesibles (requiere sesión = login hecho). */
export async function discoverTelegramChats(accountId: number): Promise<TelegramChat[]> {
  const r = await apiGet<{
    chats: { chat_id: number; name: string; kind: string; is_forum?: boolean }[]
  }>(`/accounts/${accountId}/telegram/chats`)
  return r.chats.map((c) => ({
    chatId: c.chat_id,
    name: c.name,
    kind: c.kind as TelegramChat["kind"],
    isForum: Boolean(c.is_forum),
  }))
}

export interface TelegramTopic {
  topicId: number
  title: string
}

/** Temas (forum topics) de un chat — los 'sub-canales' de un supergrupo con foros. */
export async function discoverTelegramTopics(
  accountId: number,
  chatId: number,
): Promise<TelegramTopic[]> {
  const r = await apiGet<{ topics: { topic_id: number; title: string }[] }>(
    `/accounts/${accountId}/telegram/chats/${chatId}/topics`,
  )
  return r.topics.map((t) => ({ topicId: t.topic_id, title: t.title }))
}

export type ChatMode = "anuncios" | "programa" | "intimo"

export interface AllowedChatSel {
  chatId: number
  /** topics específicos elegidos; null = todos los temas del chat. */
  topicIds: number[] | null
  /** Naturaleza del chat (qué se extrae y a quién se le crea ficha). */
  mode: ChatMode
}

export interface TelegramSourceInfo {
  sourceId: number
  config: Record<string, unknown>
  allowedChats: AllowedChatSel[]
}

/** La source telegram vinculada a la cuenta (para leer/guardar allowed_chats). null si no hay. */
export async function getTelegramSource(accountId: number): Promise<TelegramSourceInfo | null> {
  const rows = await apiGet<
    { id: number; type: string; account_id: number | null; config: Record<string, unknown> }[]
  >("/sources")
  const src = rows.find((s) => s.type === "telegram" && s.account_id === accountId)
  if (!src) return null
  const raw =
    (src.config?.["allowed_chats"] as
      | { chat_id?: unknown; topic_ids?: unknown; mode?: unknown }[]
      | undefined) ?? []
  const allowedChats: AllowedChatSel[] = raw
    .map((c) => ({
      chatId: Number(c.chat_id),
      topicIds: Array.isArray(c.topic_ids)
        ? c.topic_ids.map(Number).filter((n) => Number.isFinite(n))
        : null,
      mode: (c.mode === "programa" || c.mode === "intimo" ? c.mode : "anuncios") as ChatMode,
    }))
    .filter((c) => Number.isFinite(c.chatId))
  return { sourceId: src.id, config: src.config ?? {}, allowedChats }
}

export interface AllowedChatInput {
  chatId: number
  /** null = todos los temas; [ids] = solo esos topics. */
  topicIds?: number[] | null
  streaming?: boolean
  priority?: boolean
  mode?: ChatMode
}

// ----- Cliente local (ingesta desde la PC del usuario, vía gateway) ----- //

export interface ServerIdentity {
  userId: number
  email: string
  authEnforced: boolean
}

/** GET /auth/me — quién resuelve el server + si exige auth (define si el cliente necesita token). */
export async function fetchMe(): Promise<ServerIdentity> {
  const r = await apiGet<{ user_id: number; email: string; auth_enforced: boolean }>("/auth/me")
  return { userId: r.user_id, email: r.email, authEnforced: r.auth_enforced }
}

export interface LocalClientSource {
  sourceId: number
  name: string
  type: string
}

export interface LocalClientStatus {
  authEnforced: boolean
  /** Sources sin cuenta vinculada — las crea el gateway cuando un cliente local reporta. */
  sources: LocalClientSource[]
}

/** Estado para la tarjeta del cliente local: si el server pide auth + qué clientes reportaron. */
export async function fetchLocalClientStatus(): Promise<LocalClientStatus> {
  const [me, rows] = await Promise.all([
    fetchMe(),
    apiGet<{ id: number; name: string; type: string; account_id: number | null }[]>("/sources"),
  ])
  const sources = rows
    .filter((s) => s.account_id == null)
    .map((s) => ({ sourceId: s.id, name: s.name, type: s.type }))
  return { authEnforced: me.authEnforced, sources }
}

/** Persiste allowed_chats en el config del source (PATCH /sources/{id}; preserva el resto). */
export async function setAllowedChats(
  sourceId: number,
  chats: AllowedChatInput[],
  baseConfig: Record<string, unknown> = {},
): Promise<void> {
  const allowed_chats = chats.map((c) => ({
    chat_id: c.chatId,
    topic_ids: c.topicIds === undefined ? null : c.topicIds,
    streaming: c.streaming ?? false,
    priority: c.priority ?? false,
    mode: c.mode ?? "anuncios",
  }))
  await apiPatch<unknown>(`/sources/${sourceId}`, { config: { ...baseConfig, allowed_chats } })
}

// ----- WhatsApp: dispositivo vinculado (código por número) + catálogo/selección de chats ----- //

export type WhatsAppLinkStatus =
  | "sin_vincular"
  | "codigo"
  | "vinculado"
  | "desconectado"
  | "cerrado"
  | "baneado"

export interface WhatsAppStatus {
  status: WhatsAppLinkStatus
  connected: boolean
  phoneMasked: string
  pairCode: string | null
  lastError: string | null
  chats: number
  buffered: number
}

export async function getWhatsAppStatus(accountId: number): Promise<WhatsAppStatus> {
  const r = await apiGet<{
    status: WhatsAppLinkStatus
    connected: boolean
    phone_masked: string
    pair_code: string | null
    last_error: string | null
    chats: number
    buffered: number
  }>(`/accounts/${accountId}/whatsapp/status`)
  return {
    status: r.status,
    connected: r.connected,
    phoneMasked: r.phone_masked,
    pairCode: r.pair_code,
    lastError: r.last_error,
    chats: r.chats,
    buffered: r.buffered,
  }
}

/** Pide el código de «Vincular con número de teléfono». Dura pocos minutos. */
export async function pairWhatsApp(accountId: number, phone: string): Promise<string> {
  const r = await apiPost<{ code: string }>(`/accounts/${accountId}/whatsapp/pair`, { phone })
  return r.code
}

export interface WhatsAppChat {
  chatId: string
  name: string
  kind: "group" | "user" | "other"
  msgCount: number
  firstAt: string | null
  lastAt: string | null
  /** mensajes en la bandeja de espera (entrarían al incluir el chat). */
  buffered: number
  included: boolean
  mode: ChatMode | null
  since: string | null
}

export async function listWhatsAppChats(accountId: number): Promise<WhatsAppChat[]> {
  const r = await apiGet<{
    chats: {
      chat_id: string
      name: string
      kind: string
      msg_count: number
      first_at: string | null
      last_at: string | null
      buffered: number
      included: boolean
      mode: string | null
      since: string | null
    }[]
  }>(`/accounts/${accountId}/whatsapp/chats`)
  return r.chats.map((c) => ({
    chatId: c.chat_id,
    name: c.name,
    kind: c.kind === "group" || c.kind === "user" ? c.kind : "other",
    msgCount: c.msg_count,
    firstAt: c.first_at,
    lastAt: c.last_at,
    buffered: c.buffered,
    included: c.included,
    mode: c.mode === "programa" || c.mode === "intimo" || c.mode === "anuncios" ? c.mode : null,
    since: c.since,
  }))
}

export interface WhatsAppChatPick {
  chatId: string
  name: string
  mode: ChatMode
  /** YYYY-MM-DD; null = todo lo que haya. */
  since: string | null
}

export async function saveWhatsAppChats(
  accountId: number,
  picks: WhatsAppChatPick[],
): Promise<{ saved: number; ingested: number }> {
  return apiPost<{ saved: number; ingested: number }>(`/accounts/${accountId}/whatsapp/chats`, {
    allowed: picks.map((p) => ({ chat_id: p.chatId, name: p.name, mode: p.mode, since: p.since })),
  })
}

export async function unlinkWhatsApp(accountId: number): Promise<void> {
  await apiPost<{ status: string }>(`/accounts/${accountId}/whatsapp/unlink`)
}
