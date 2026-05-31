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

/** Sello "act. hace Xs" con su propio tick de 1 s (no re-renderiza todo el árbol). */
export function LiveSince() {
  const { lastRefreshed } = useAutoRefresh()
  const [, force] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => force((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])
  const secs = Math.max(0, Math.round((Date.now() - lastRefreshed.getTime()) / 1000))
  const label = secs < 60 ? `hace ${secs} s` : `hace ${Math.round(secs / 60)} min`
  return <span className="num text-xs text-muted-foreground">act. {label}</span>
}
