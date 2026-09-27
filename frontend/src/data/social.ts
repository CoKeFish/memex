// Redes sociales monitoreadas (Apify) contra la API real. Un "ingestor por red" (instagram/
// facebook/x) cuyo `config.accounts` es el allowlist de cuentas seguidas. Add/quitar cuenta pega a
// POST/DELETE /sources/{id}/social/accounts (valida + normaliza server-side). camelCase como el resto.

import { apiDelete, apiGet, apiPost } from "@/lib/api"

export type SocialPlatform = "instagram" | "facebook" | "x"
export const SOCIAL_PLATFORMS: SocialPlatform[] = ["instagram", "facebook", "x"]

export const SOCIAL_PLATFORM_LABEL: Record<SocialPlatform, string> = {
  instagram: "Instagram",
  facebook: "Facebook",
  x: "X (Twitter)",
}

export interface FollowedAccount {
  account: string
  priority: boolean
}

export interface SocialSource {
  id: number
  name: string
  type: SocialPlatform
  enabled: boolean
  /** Cuenta del vault vinculada (tiene el token de Apify); null si no se vinculó. */
  accountId: number | null
  accountAlias: string | null
  /** Allowlist de cuentas seguidas (de `config.accounts`). */
  accounts: FollowedAccount[]
  /** Si baja fotos + video a MinIO/OCR (`config.extract_media`). */
  extractMedia: boolean
}

interface SourceApi {
  id: number
  user_id: number
  name: string
  type: string
  enabled: boolean
  config: Record<string, unknown>
  created_at: string
  account_id: number | null
  account_alias: string | null
}

function toFollowed(raw: unknown): FollowedAccount | null {
  if (typeof raw !== "object" || raw === null) return null
  const r = raw as Record<string, unknown>
  const account = String(r.account ?? "").trim()
  if (!account) return null
  return { account, priority: Boolean(r.priority) }
}

function toSocialSource(s: SourceApi): SocialSource {
  const cfg = (s.config ?? {}) as Record<string, unknown>
  const rawAccounts = Array.isArray(cfg.accounts) ? cfg.accounts : []
  const accounts = rawAccounts.map(toFollowed).filter((a): a is FollowedAccount => a !== null)
  return {
    id: s.id,
    name: s.name,
    type: s.type as SocialPlatform,
    enabled: s.enabled,
    accountId: s.account_id,
    accountAlias: s.account_alias,
    accounts,
    extractMedia: Boolean(cfg.extract_media),
  }
}

function isSocial(t: string): t is SocialPlatform {
  return (SOCIAL_PLATFORMS as string[]).includes(t)
}

/** Sources sociales (instagram/facebook/x) con su allowlist de cuentas seguidas. */
export async function fetchSocialSources(): Promise<SocialSource[]> {
  const rows = await apiGet<SourceApi[]>("/sources")
  return rows.filter((s) => isSocial(s.type)).map(toSocialSource)
}

/** Agrega una cuenta seguida (handle/URL; el backend normaliza). Devuelve la source actualizada. */
export async function addFollowedAccount(
  sourceId: number,
  handle: string,
  priority = false,
): Promise<SocialSource> {
  return toSocialSource(
    await apiPost<SourceApi>(`/sources/${sourceId}/social/accounts`, { handle, priority }),
  )
}

/** Quita una cuenta seguida del allowlist. Devuelve la source actualizada. */
export async function removeFollowedAccount(
  sourceId: number,
  handle: string,
): Promise<SocialSource> {
  return toSocialSource(
    await apiDelete<SourceApi>(`/sources/${sourceId}/social/accounts/${encodeURIComponent(handle)}`),
  )
}
