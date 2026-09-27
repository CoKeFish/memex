import { ArrowDownRight, ArrowUpRight } from "lucide-react"
import { cn } from "@/lib/utils"
import { formatPct } from "@/lib/format"

/** Variación porcentual. invert=true ⇒ subir es malo (costo): rojo al subir. `noBase` = el periodo
 *  previo no es comparable (sin datos / costo 0) → muestra "sin base" en vez de un % engañoso. */
export function Delta({
  value,
  invert = true,
  noBase = false,
  className,
}: {
  value: number | null
  invert?: boolean
  noBase?: boolean
  className?: string
}) {
  if (noBase) {
    return (
      <span className={cn("num text-xs text-muted-foreground", className)} title="sin periodo previo comparable">
        sin base
      </span>
    )
  }
  if (value === null || !Number.isFinite(value)) {
    return <span className={cn("num text-xs text-muted-foreground", className)}>—</span>
  }
  const up = value >= 0
  const bad = invert ? up : !up
  const mag = Math.abs(value)
  // % gigante (>999%) contra una base chica → factor "×N", más legible y honesto que "3033%".
  const label = mag > 9.99 ? `×${Math.round(1 + mag)}` : formatPct(mag, 0)
  return (
    <span
      className={cn(
        "num inline-flex items-center gap-0.5 text-xs font-medium",
        bad ? "text-status-error" : "text-status-ok",
        className,
      )}
      title={`${(value * 100).toFixed(0)}%`}
    >
      {up ? <ArrowUpRight className="size-3" /> : <ArrowDownRight className="size-3" />}
      {label}
    </span>
  )
}

/** Mini-gráfico de línea para tarjetas KPI. */
export function Sparkline({
  data,
  stroke = "var(--brand)",
  className,
}: {
  data: number[]
  stroke?: string
  className?: string
}) {
  if (data.length < 2) return null
  const w = 100
  const h = 28
  const max = Math.max(...data, 1)
  const min = Math.min(...data, 0)
  const span = max - min || 1
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w
      const y = h - ((v - min) / span) * h
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(" ")
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={cn("h-7 w-full", className)} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
