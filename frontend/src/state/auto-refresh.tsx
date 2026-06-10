import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

export type IntervalSec = 0 | 30 | 60 | 300

interface RefreshCtx {
  intervalSec: IntervalSec
  setIntervalSec: (s: IntervalSec) => void
  /** Momento de referencia del dashboard; avanza en cada refresco (auto o manual). */
  now: Date
  lastRefreshed: Date
  refreshNow: () => void
}

const Ctx = createContext<RefreshCtx | null>(null)
const KEY = "memex.refresh"

export function AutoRefreshProvider({ children }: { children: ReactNode }) {
  const [intervalSec, setIntervalSecState] = useState<IntervalSec>(
    () => (Number(localStorage.getItem(KEY)) as IntervalSec) || 0,
  )
  const [now, setNow] = useState<Date>(() => new Date())

  function refreshNow() {
    setNow(new Date())
  }

  function setIntervalSec(s: IntervalSec) {
    setIntervalSecState(s)
    localStorage.setItem(KEY, String(s))
  }

  useEffect(() => {
    if (intervalSec === 0) return
    const id = window.setInterval(() => {
      if (!document.hidden) setNow(new Date())
    }, intervalSec * 1000)
    return () => window.clearInterval(id)
  }, [intervalSec])

  return (
    <Ctx.Provider value={{ intervalSec, setIntervalSec, now, lastRefreshed: now, refreshNow }}>
      {children}
    </Ctx.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAutoRefresh(): RefreshCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useAutoRefresh debe usarse dentro de AutoRefreshProvider")
  return c
}
