// Sección del lote por ventanas de /procesamiento (espejo UX de BackfillSection en ingesta):
// progreso del snapshot (frontera/total + gasto), avanzar UNA ventana o el resto, historial con
// costo POR VENTANA, y defaults de tamaño por medio. El avance corre en background como una
// corrida más; el padre (ManualRunPanel) es dueño del polling y pasa `disabled` mientras corre.

import { useState } from "react"
import { CheckCircle2, FastForward, Loader2, Play, RotateCcw } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { formatDurationMs, formatInt, formatUsd, formatUsdFine } from "@/lib/format"
import { KIND_LABELS } from "@/lib/inbox-format"
import { advanceLot, deleteLot, type LotState, type LotWindow, patchWindowDefaults } from "@/data"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

/** Resumen legible de los filtros con los que se congeló el snapshot. */
function lotFilterSummary(lot: LotState): string {
  const f = lot.filters as {
    source_id?: number | null
    since?: string | null
    until?: string | null
    limit?: number | null
    only?: string | null
  }
  const parts: string[] = [f.source_id != null ? `fuente #${f.source_id}` : "todas las fuentes"]
  if (f.since) parts.push(`desde ${f.since}`)
  if (f.until) parts.push(`hasta ${f.until}`)
  if (f.limit) parts.push(`tope ${formatInt(f.limit)}`)
  if (f.only) parts.push(String(f.only))
  if (lot.force) parts.push("force")
  return parts.join(" · ")
}

/** Una ventana ejecutada del historial: rango (1-based), resultados, costo y duración. */
function WindowRow({ w, index }: { w: LotWindow; index: number }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-1.5 text-xs">
      <span className="num text-muted-foreground">#{index}</span>
      <span className="num">
        {formatInt(w.startIdx + 1)}–{formatInt(w.endIdx)}
      </span>
      <span className="num text-muted-foreground">{formatInt(w.n)} msj</span>
      {w.errors > 0 && <span className="num text-status-error">{formatInt(w.errors)} err</span>}
      <span className="num ml-auto" title={formatUsdFine(w.costUsd)}>
        {formatUsd(w.costUsd)}
      </span>
      <span className="num text-muted-foreground">{formatDurationMs(w.msElapsed)}</span>
    </div>
  )
}

/** Editor inline de los defaults de tamaño de ventana por medio (PATCH al salir del campo).
 * Lot-agnóstico: el caller le pasa los defaults (del lote o de GET /processing/window-defaults). */
export function DefaultsEditor({
  defaults,
  disabled,
  onChanged,
}: {
  defaults: Record<string, number>
  disabled: boolean
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)

  async function save(kind: string, value: number) {
    if (value < 1 || value === defaults[kind]) return
    setBusy(true)
    try {
      await patchWindowDefaults({ [kind]: value })
      toast.success(`Default de ${KIND_LABELS[kind] ?? kind}: ${value} msj/ventana`)
      onChanged()
    } catch (e) {
      toast.error("No se pudo guardar el default", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-muted-foreground">
      <span title="tamaño de ventana sugerido al crear un lote, según el medio de la fuente">
        ventana por medio
      </span>
      {Object.entries(defaults).map(([kind, size]) => (
        <label key={kind} className="flex items-center gap-1.5">
          {KIND_LABELS[kind] ?? kind}
          <Input
            type="number"
            min={1}
            defaultValue={size}
            disabled={disabled || busy}
            onBlur={(e) => void save(kind, Number(e.target.value))}
            className="h-7 w-16 text-xs"
          />
        </label>
      ))}
    </div>
  )
}

/**
 * Estado + controles del lote ya creado. El padre crea el lote (reusa su form de filtros), hace el
 * polling mientras `lot.busy` y nos pasa `disabled` (corrida en curso / otra acción del panel).
 */
export function LotSection({
  lot,
  disabled,
  onChanged,
}: {
  lot: LotState
  disabled: boolean
  onChanged: () => void
}) {
  const [busy, setBusy] = useState<null | "window" | "rest" | "reset">(null)
  // Override del tamaño de la PRÓXIMA ventana (null → usar el default guardado del lote).
  const [ovSize, setOvSize] = useState<number | null>(null)
  const curSize = ovSize ?? lot.windowSize

  const pct = lot.total ? lot.frontier / lot.total : 0
  const blocked = disabled || busy !== null || lot.busy

  async function advance(rest: boolean) {
    setBusy(rest ? "rest" : "window")
    try {
      const r = await advanceLot({ rest, windowSize: ovSize })
      setOvSize(null)
      if (r.status === "done") {
        toast.info("El lote ya está completo")
      } else if (r.window) {
        toast.success(
          `Ventana en curso: ${formatInt(r.window.startIdx + 1)}–${formatInt(r.window.endIdx)}`,
          { description: "corre en background; mirá el avance acá y en Corridas recientes" },
        )
      } else {
        toast.success("Procesando el resto del lote", {
          description: "ventana a ventana, en background",
        })
      }
      onChanged()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.warning("Ya hay una corrida en curso", { description: "Esperá a que termine." })
      } else {
        toast.error("No se pudo avanzar el lote", { description: errMsg(e) })
      }
    } finally {
      setBusy(null)
    }
  }

  async function reset() {
    setBusy("reset")
    try {
      await deleteLot()
      toast.success("Lote borrado")
      onChanged()
    } catch (e) {
      toast.error("No se pudo borrar el lote", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="eyebrow">Lote activo</div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            {lot.stages.join(" → ")} · {lotFilterSummary(lot)}
          </div>
        </div>
        {lot.busy && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" /> ventana en curso…
          </span>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
          <span className="num">
            {formatInt(lot.frontier)}/{formatInt(lot.total)} mensajes
          </span>
          <span className="num">
            <span title={formatUsdFine(lot.spentUsd)}>{formatUsd(lot.spentUsd)} gastado</span>
            {" · "}
            {Math.round(pct * 100)}%
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-full rounded-full transition-all",
              lot.status === "done" ? "bg-status-ok" : "bg-brand",
            )}
            style={{ width: `${pct * 100}%` }}
          />
        </div>
      </div>

      {lot.status === "done" ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-1.5 text-sm text-status-ok">
            <CheckCircle2 className="size-4" /> Lote completo
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto"
            disabled={blocked}
            onClick={reset}
          >
            <RotateCcw className="size-3.5" /> Reiniciar
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            ventana
            <Input
              type="number"
              min={1}
              value={curSize}
              disabled={blocked}
              onChange={(e) => setOvSize(Math.max(1, Number(e.target.value)))}
              className="h-8 w-20 text-xs"
              title="mensajes por ventana; el cambio queda como nuevo default del lote"
            />
            msj
          </label>
          <Button size="sm" disabled={blocked} onClick={() => void advance(false)}>
            {busy === "window" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Play className="size-3.5" />
            )}
            Procesar ventana
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={blocked}
            onClick={() => void advance(true)}
          >
            {busy === "rest" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <FastForward className="size-3.5" />
            )}
            Procesar el resto
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto"
            disabled={blocked}
            onClick={reset}
          >
            <RotateCcw className="size-3.5" /> Reiniciar
          </Button>
        </div>
      )}

      <DefaultsEditor defaults={lot.defaults} disabled={blocked} onChanged={onChanged} />

      {lot.history.length > 0 && (
        <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
          {[...lot.history].reverse().map((w, i) => (
            <WindowRow key={`${w.startIdx}-${w.at}`} w={w} index={lot.history.length - i} />
          ))}
        </div>
      )}
    </div>
  )
}
