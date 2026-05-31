import { createContext, useContext, useMemo, useState, type ReactNode } from "react"
import { getReviewItems, getSeedAlerts } from "@/data"
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
  const [alerts, setAlerts] = useState<AlertEvent[]>(() => getSeedAlerts().map((a) => ({ ...a })))

  const value = useMemo<AlertsCtx>(
    () => ({
      alerts,
      unread: alerts.filter((a) => !a.read).length,
      reviewCount: getReviewItems().length,
      markRead: (id) => setAlerts((prev) => prev.map((a) => (a.id === id ? { ...a, read: true } : a))),
      markAllRead: () => setAlerts((prev) => prev.map((a) => ({ ...a, read: true }))),
      addAlert: (a) => setAlerts((prev) => [a, ...prev]),
    }),
    [alerts],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAlerts(): AlertsCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useAlerts debe usarse dentro de AlertsProvider")
  return c
}
