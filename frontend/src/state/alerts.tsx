import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import {
  dismissNotification,
  fetchAlerts,
  fetchNotifications,
  fetchOverview,
  markNotificationRead,
  readAllNotifications,
  toAlertEvent,
} from "@/data"
import type { AlertEvent, PersistedNotification } from "@/types/domain"

interface AlertsCtx {
  alerts: AlertEvent[]
  unread: number
  reviewCount: number
  markRead: (id: string) => void
  markAllRead: () => void
  /** Descarta un aviso de la campana. Solo aplica a los persistidos (no-op en los dinámicos). */
  dismiss: (id: string) => void
  addAlert: (a: AlertEvent) => void
}

const Ctx = createContext<AlertsCtx | null>(null)

const NOTIF_PREFIX = "notif:"

export function AlertsProvider({ children }: { children: ReactNode }) {
  // Dos fuentes en una campana: alertas DINÁMICAS (/stats/alerts; "leído" en cliente vía readIds) y
  // avisos PERSISTIDOS (cola /notifications; "leído"/"descartado" en servidor, optimista sobre este
  // estado).
  const [raw, setRaw] = useState<AlertEvent[]>([])
  const [persisted, setPersisted] = useState<PersistedNotification[]>([])
  const [reviewCount, setReviewCount] = useState(0)
  const [readIds, setReadIds] = useState<Set<string>>(() => new Set())

  useEffect(() => {
    let alive = true
    // allSettled: un 404 de /notifications (durante el rollout, antes del redeploy) NO debe blanquear
    // las alertas dinámicas ni el overview.
    void Promise.allSettled([fetchAlerts(), fetchOverview(), fetchNotifications()]).then((res) => {
      if (!alive) return
      if (res[0].status === "fulfilled") setRaw(res[0].value)
      if (res[1].status === "fulfilled") setReviewCount(res[1].value.review.total)
      if (res[2].status === "fulfilled") setPersisted(res[2].value.items)
    })
    return () => {
      alive = false
    }
  }, [])

  const markRead = useCallback((id: string) => {
    if (id.startsWith(NOTIF_PREFIX)) {
      const notifId = Number(id.slice(NOTIF_PREFIX.length))
      void markNotificationRead(notifId).catch(() => {})
      const at = new Date().toISOString()
      setPersisted((prev) =>
        prev.map((n) => (n.id === notifId && n.readAt === null ? { ...n, readAt: at } : n)),
      )
    } else {
      setReadIds((prev) => new Set(prev).add(id))
    }
  }, [])

  const dismiss = useCallback((id: string) => {
    if (!id.startsWith(NOTIF_PREFIX)) return // descartar solo aplica a avisos persistidos
    const notifId = Number(id.slice(NOTIF_PREFIX.length))
    void dismissNotification(notifId).catch(() => {})
    setPersisted((prev) => prev.filter((n) => n.id !== notifId))
  }, [])

  const markAllRead = useCallback(() => {
    setReadIds(new Set(raw.map((a) => a.id)))
    void readAllNotifications().catch(() => {})
    const at = new Date().toISOString()
    setPersisted((prev) => prev.map((n) => (n.readAt === null ? { ...n, readAt: at } : n)))
  }, [raw])

  const value = useMemo<AlertsCtx>(() => {
    const dynamic = raw.map((a) => ({ ...a, read: a.read || readIds.has(a.id) }))
    const fromQueue = persisted.map(toAlertEvent)
    // Cronológico newest-first; `at` es ISO 8601 → comparación lexicográfica = temporal.
    const alerts = [...dynamic, ...fromQueue].sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0))
    return {
      alerts,
      unread: alerts.filter((a) => !a.read).length,
      reviewCount,
      markRead,
      markAllRead,
      dismiss,
      addAlert: (a) => setRaw((prev) => [a, ...prev]),
    }
  }, [raw, persisted, reviewCount, readIds, markRead, markAllRead, dismiss])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAlerts(): AlertsCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useAlerts debe usarse dentro de AlertsProvider")
  return c
}
