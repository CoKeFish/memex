import { createContext, useContext, type ReactNode } from "react"
import { useSearchParams } from "react-router-dom"
import { RANGES, type RangeKey } from "@/lib/selectors"

interface TimeRangeCtx {
  range: RangeKey
  setRange: (r: RangeKey) => void
}

const Ctx = createContext<TimeRangeCtx | null>(null)
const KEY = "memex.range"
const VALID = new Set(RANGES.map((r) => r.key))

function isRange(v: string | null): v is RangeKey {
  return v !== null && VALID.has(v as RangeKey)
}

export function TimeRangeProvider({ children }: { children: ReactNode }) {
  const [params, setParams] = useSearchParams()
  const fromUrl = params.get("range")
  const stored = localStorage.getItem(KEY)
  const range: RangeKey = isRange(fromUrl) ? fromUrl : isRange(stored) ? stored : "7d"

  function setRange(r: RangeKey) {
    localStorage.setItem(KEY, r)
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.set("range", r)
        return next
      },
      { replace: true },
    )
  }

  return <Ctx.Provider value={{ range, setRange }}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTimeRange(): TimeRangeCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useTimeRange debe usarse dentro de TimeRangeProvider")
  return c
}
