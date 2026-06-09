import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { fetchAlerts, fetchOverview } from "@/data"
import type { AlertEvent } from "@/types/domain"

interface AlertsCtx {
  alerts: AlertEvent[]
  unread: number
  reviewCount: number
  markRead: (id: string) => void
  markAllRead: () => void
  addAlert: (a: AlertEvent) => void
}

const Ctx = createContext<AlertsCtx | null>(null)

export function AlertsProvider({ children }: { children: ReactNode }) {
  const [raw, setRaw] = useState<AlertEvent[]>([])
  const [reviewCount, setReviewCount] = useState(0)
  // El estado "leído" vive en el cliente: las alertas reales se re-derivan en cada carga.
  const [readIds, setReadIds] = useState<Set<string>>(() => new Set())

  useEffect(() => {
    let alive = true
    Promise.all([fetchAlerts(), fetchOverview()])
      .then(([a, ov]) => {
        if (!alive) return
        setRaw(a)
        setReviewCount(ov.review.total)
      })
      .catch(() => {
        /* sin datos → bandeja vacía (honesto), nunca mock */
      })
    return () => {
      alive = false
    }
  }, [])

  const value = useMemo<AlertsCtx>(() => {
    const alerts = raw.map((a) => ({ ...a, read: a.read || readIds.has(a.id) }))
    return {
      alerts,
      unread: alerts.filter((a) => !a.read).length,
      reviewCount,
      markRead: (id) => setReadIds((prev) => new Set(prev).add(id)),
      markAllRead: () => setReadIds(new Set(raw.map((a) => a.id))),
      addAlert: (a) => setRaw((prev) => [a, ...prev]),
    }
  }, [raw, reviewCount, readIds])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAlerts(): AlertsCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useAlerts debe usarse dentro de AlertsProvider")
  return c
}
