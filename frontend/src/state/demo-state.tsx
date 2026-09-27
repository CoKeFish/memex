import { createContext, useContext, useState, type ReactNode } from "react"

// Estado de demo global para mostrar los estados vacío/carga/error de forma consistente
// en todos los paneles sin backend. "ready" = datos mock normales.
export type DemoState = "ready" | "loading" | "empty" | "error"

interface DemoStateCtx {
  state: DemoState
  setState: (s: DemoState) => void
}

const Ctx = createContext<DemoStateCtx | null>(null)

export function DemoStateProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DemoState>("ready")
  return <Ctx.Provider value={{ state, setState }}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useDemoState(): DemoStateCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useDemoState debe usarse dentro de DemoStateProvider")
  return c
}
