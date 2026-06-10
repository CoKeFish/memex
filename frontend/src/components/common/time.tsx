import { useEffect, useState } from "react"
import { useAutoRefresh } from "@/state/auto-refresh"
import { ageBucket, formatRelative } from "@/lib/format"
import { freshnessTone } from "@/lib/status"
import { Led } from "./led"

export function RelativeTime({ date, className }: { date: string; className?: string }) {
  const { now } = useAutoRefresh()
  return (
    <span className={className} title={new Date(date).toLocaleString("es-MX")}>
      {formatRelative(date, now)}
    </span>
  )
}

export function FreshnessDot({ date, pulse }: { date: string; pulse?: boolean }) {
  const { now } = useAutoRefresh()
  return <Led tone={freshnessTone[ageBucket(date, now)]} pulse={pulse} />
}

/** Sello "act. hace Xs" con su propio tick de 1 s (no re-renderiza todo el árbol).
 * Los segundos viven en estado y se calculan en el callback del interval — el render queda puro
 * (sin Date.now()); al refrescar, el reset a 0 se ajusta durante el render, no en un effect. */
export function LiveSince() {
  const { lastRefreshed } = useAutoRefresh()
  const [secs, setSecs] = useState(0)
  const [prevRefreshed, setPrevRefreshed] = useState(lastRefreshed)
  if (prevRefreshed !== lastRefreshed) {
    setPrevRefreshed(lastRefreshed)
    setSecs(0)
  }
  useEffect(() => {
    const id = window.setInterval(
      () => setSecs(Math.max(0, Math.round((Date.now() - lastRefreshed.getTime()) / 1000))),
      1000,
    )
    return () => window.clearInterval(id)
  }, [lastRefreshed])
  const label = secs < 60 ? `hace ${secs} s` : `hace ${Math.round(secs / 60)} min`
  return <span className="num text-xs text-muted-foreground">act. {label}</span>
}
