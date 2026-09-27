import { createContext, useContext, useState, type ReactNode } from "react"
import { activeDisplayTz, autodetectedTz, setDisplayTzOverride } from "@/lib/timezone"

// Zona horaria de display de /metricas. Fuente de verdad = override (localStorage) ?? autodetectada.
// El provider existe para re-renderizar el selector y los consumidores cuando cambia; las funciones
// puras de ventana (presetWindow/customWindow) reciben la TZ por argumento, no leen el contexto.

interface MetricsTzCtx {
  /** TZ activa (override o autodetectada). */
  tz: string
  /** TZ autodetectada del navegador (para etiquetar la opción "auto"). */
  autodetected: string
  /** Cambia la TZ; si coincide con la autodetectada, limpia el override (vuelve a "auto"). */
  setTz: (tz: string) => void
}

const Ctx = createContext<MetricsTzCtx | null>(null)

export function MetricsTzProvider({ children }: { children: ReactNode }) {
  const [tz, setTzState] = useState<string>(() => activeDisplayTz())
  const autodetected = autodetectedTz()

  function setTz(next: string) {
    setDisplayTzOverride(next === autodetected ? null : next)
    setTzState(next)
  }

  return <Ctx.Provider value={{ tz, autodetected, setTz }}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useMetricsTz(): MetricsTzCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useMetricsTz debe usarse dentro de MetricsTzProvider")
  return c
}
