import { useState } from "react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { customWindow, presetWindow, type MetricsWindow, type RangePreset } from "@/data"

const PRESETS: { key: RangePreset; label: string }[] = [
  { key: "today", label: "Hoy" },
  { key: "7d", label: "7 días" },
  { key: "30d", label: "30 días" },
  { key: "90d", label: "90 días" },
  { key: "all", label: "Todo" },
]

/**
 * Control de rango LOCAL de /metricas (presets + rango personalizado). No toca el range-picker
 * global del topbar: la vista de métricas necesita rango a medida (since/until) que el global no da.
 * Emite una `MetricsWindow` al padre, que la pasa al rollup y a la auditoría.
 */
export function MetricsFilters({ onChange }: { onChange: (w: MetricsWindow) => void }) {
  const [active, setActive] = useState<RangePreset | "custom">("30d")
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")

  function pickPreset(p: RangePreset) {
    setActive(p)
    onChange(presetWindow(p))
  }

  function applyCustom(s: string, u: string) {
    setSince(s)
    setUntil(u)
    if (s || u) {
      setActive("custom")
      onChange(customWindow(s || undefined, u || undefined))
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            onClick={() => pickPreset(p.key)}
            className={cn(
              "rounded px-2.5 py-1 text-xs font-medium transition-colors",
              active === p.key
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div
        className={cn(
          "flex items-center gap-1.5 rounded-md border border-border px-1.5 py-0.5",
          active === "custom" ? "border-brand/50" : "",
        )}
      >
        <Input
          type="date"
          value={since}
          max={until || undefined}
          onChange={(e) => applyCustom(e.target.value, until)}
          className="h-7 w-[132px] border-0 px-1 text-xs shadow-none focus-visible:ring-0"
          aria-label="Desde"
        />
        <span className="text-xs text-muted-foreground">→</span>
        <Input
          type="date"
          value={until}
          min={since || undefined}
          onChange={(e) => applyCustom(since, e.target.value)}
          className="h-7 w-[132px] border-0 px-1 text-xs shadow-none focus-visible:ring-0"
          aria-label="Hasta"
        />
      </div>
    </div>
  )
}
