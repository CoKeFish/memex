import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

export type IntervalSec = 0 | 30 | 60 | 300

interface RefreshCtx {
  intervalSec: IntervalSec
  setIntervalSec: (s: IntervalSec) => void
  /** Momento de referencia de los DATOS; avanza en cada refresco (auto o manual) y dispara los
   * refetch de `useAsync`. Con el auto-refresh apagado queda quieto a propósito. */
  now: Date
  /** Reloj VIVO para mostrar tiempos relativos («hace 3 min»): avanza cada 30 s aunque el
   * auto-refresh esté apagado. Sin esto, con el refresco en 0 `now` se congelaba al cargar y todo
   * lo posterior aparecía «en 16 min» (en el futuro). No dispara refetch. */
  clock: Date
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
  const [clock, setClock] = useState<Date>(() => new Date())

  function refreshNow() {
    const t = new Date()
    setNow(t)
    setClock(t)
  }

  // Reloj de display: independiente del intervalo de datos; se pausa con la pestaña oculta.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (!document.hidden) setClock(new Date())
    }, 30_000)
    return () => window.clearInterval(id)
  }, [])

  function setIntervalSec(s: IntervalSec) {
    setIntervalSecState(s)
    localStorage.setItem(KEY, String(s))
  }

  useEffect(() => {
    if (intervalSec === 0) return
    const id = window.setInterval(() => {
      if (!document.hidden) {
        const t = new Date()
        setNow(t)
        setClock(t)
      }
    }, intervalSec * 1000)
    return () => window.clearInterval(id)
  }, [intervalSec])

  return (
    <Ctx.Provider
      value={{ intervalSec, setIntervalSec, now, clock, lastRefreshed: now, refreshNow }}
    >
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
