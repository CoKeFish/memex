import { useState } from "react"
import { Globe } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { customWindow, presetWindow, type MetricsWindow, type RangePreset } from "@/data"
import { useMetricsTz } from "@/state/metrics-tz"

const PRESETS: { key: RangePreset; label: string }[] = [
  { key: "today", label: "Hoy" },
  { key: "7d", label: "7 días" },
  { key: "30d", label: "30 días" },
  { key: "90d", label: "90 días" },
  { key: "all", label: "Todo" },
]

//: Opciones de TZ del selector (además de la autodetectada, que se inyecta primera).
const TZ_OPTIONS = ["America/Bogota", "America/Mexico_City", "America/New_York", "UTC"]

/** "America/Bogota" → "Bogota"; marca la autodetectada con "· auto". */
function tzLabel(tz: string, auto: string): string {
  const city = tz.split("/").pop()?.replace(/_/g, " ") ?? tz
  return tz === auto ? `${city} · auto` : city
}

/**
 * Control de rango LOCAL de /metricas (presets + rango personalizado + zona horaria). No toca el
 * range-picker global del topbar: la vista de métricas necesita rango a medida (since/until) que el
 * global no da. Emite una `MetricsWindow` (con `tz`) al padre, que la pasa al rollup y a la auditoría.
 */
export function MetricsFilters({ onChange }: { onChange: (w: MetricsWindow) => void }) {
  const { tz, autodetected, setTz } = useMetricsTz()
  const [active, setActive] = useState<RangePreset | "custom">("30d")
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")

  function pickPreset(p: RangePreset) {
    setActive(p)
    setSince("") // un preset limpia el rango custom (la UI no debe mostrar fechas viejas)
    setUntil("")
    onChange(presetWindow(p, tz))
  }

  function applyCustom(s: string, u: string) {
    setSince(s)
    setUntil(u)
    if (s || u) {
      setActive("custom")
      onChange(customWindow(s || undefined, u || undefined, tz))
    } else {
      // Limpiar AMBAS fechas = sin filtro → equivale a "Todo" (no dejar la ventana vieja pegada).
      setActive("all")
      onChange(presetWindow("all", tz))
    }
  }

  function changeTz(next: string) {
    setTz(next)
    // Re-emitir la ventana vigente con la nueva TZ (recomputa "hoy"/custom en su medianoche).
    if (active === "custom") onChange(customWindow(since || undefined, until || undefined, next))
    else onChange(presetWindow(active, next))
  }

  const tzOptions = Array.from(new Set([autodetected, ...TZ_OPTIONS]))

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
      <Select value={tz} onValueChange={changeTz}>
        <SelectTrigger
          className="h-8 w-auto gap-1.5 text-xs"
          aria-label="Zona horaria del día"
          title="Zona horaria que define 'hoy' y los días del eje (autodetectada; cambiala si hace falta)"
        >
          <Globe className="size-3.5 text-muted-foreground" />
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {tzOptions.map((o) => (
            <SelectItem key={o} value={o} className="text-xs">
              {tzLabel(o, autodetected)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
