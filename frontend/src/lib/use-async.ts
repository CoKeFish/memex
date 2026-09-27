import { useCallback, useEffect, useState } from "react"
import { useAutoRefresh } from "@/state/auto-refresh"

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** Re-dispara la carga manualmente (además del tick de auto-refresh). */
  reload: () => void
}

interface Internal<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/**
 * Corre una función async y expone {data, error, loading, reload}. Se re-ejecuta cuando cambian
 * los `deps`, cuando se llama `reload()`, o en el tick global de auto-refresh (`useAutoRefresh`).
 *
 * Estrategia stale-while-revalidate: `loading` arranca en true y solo se resuelve desde los
 * callbacks async (nunca con un setState síncrono dentro del efecto). En los refetch posteriores
 * se mantienen los datos previos visibles sin parpadeo. Ignora resultados obsoletos (evita races).
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const { now } = useAutoRefresh()
  const [state, setState] = useState<Internal<T>>({ data: null, error: null, loading: true })
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let cancelled = false
    fn()
      .then((d) => {
        if (!cancelled) setState({ data: d, error: null, loading: false })
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setState({ data: null, error: e instanceof Error ? e.message : String(e), loading: false })
        }
      })
    return () => {
      cancelled = true
    }
    // `fn` se omite a propósito (nueva closure por render); el control es deps/tick/now.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, now])

  return { ...state, reload }
}
