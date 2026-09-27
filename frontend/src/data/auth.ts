// Autenticación del dashboard contra la API real (cookie de sesión httpOnly).
// La contraseña solo autoriza el dashboard; el vault se descifra server-side con la master key.

import { apiGet, apiPost } from "@/lib/api"

export interface AuthIdentity {
  userId: number
  email: string
  displayName: string | null
  authEnforced: boolean
}

interface MeApi {
  user_id: number
  email: string
  display_name: string | null
  auth_enforced: boolean
}

function toIdentity(m: MeApi): AuthIdentity {
  return {
    userId: m.user_id,
    email: m.email,
    displayName: m.display_name,
    authEnforced: m.auth_enforced,
  }
}

/** Identidad de la sesión actual (GET /auth/me). Lanza ApiError 401 si no hay sesión. */
export async function fetchMe(): Promise<AuthIdentity> {
  return toIdentity(await apiGet<MeApi>("/auth/me"))
}

export async function login(email: string, password: string): Promise<AuthIdentity> {
  return toIdentity(await apiPost<MeApi>("/auth/login", { email, password }))
}

export async function signup(
  email: string,
  password: string,
  displayName?: string,
): Promise<AuthIdentity> {
  return toIdentity(
    await apiPost<MeApi>("/auth/signup", {
      email,
      password,
      display_name: displayName ?? null,
    }),
  )
}

export async function logout(): Promise<void> {
  await apiPost<void>("/auth/logout")
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await apiPost<void>("/auth/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  })
}
