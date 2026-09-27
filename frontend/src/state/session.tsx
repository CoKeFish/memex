// Estado de sesión global. Resuelve la identidad vía GET /auth/me al montar.
// En dev (auth_enforced=false) el backend devuelve siempre user 1 → no hace falta login.
// En prod sin sesión, /auth/me da 401 → user=null → el guard manda a /login.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react"
import { type AuthIdentity, fetchMe } from "@/data/auth"

interface SessionState {
  user: AuthIdentity | null
  loading: boolean
  refresh: () => Promise<void>
  setUser: (u: AuthIdentity | null) => void
}

const SessionContext = createContext<SessionState | null>(null)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthIdentity | null>(null)
  const [loading, setLoading] = useState(true)

  // El loading arranca en true (useState inicial). Seteamos estado SOLO dentro de los callbacks
  // .then/.catch (no síncrono dentro del efecto) — mismo criterio que `useAsync`.
  const refresh = useCallback((): Promise<void> => {
    return fetchMe()
      .then((u) => {
        setUser(u)
        setLoading(false)
      })
      .catch(() => {
        setUser(null)
        setLoading(false)
      })
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <SessionContext.Provider value={{ user, loading, refresh, setUser }}>
      {children}
    </SessionContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSession(): SessionState {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error("useSession debe usarse dentro de <SessionProvider>")
  return ctx
}
