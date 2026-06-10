import { useState } from "react"
import { Link } from "react-router-dom"
import { ArrowRight, CheckCircle2, FastForward, FlaskConical, Loader2, Play, RotateCcw } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { CollapsiblePanel } from "@/components/common/collapsible-panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { formatInt } from "@/lib/format"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import {
  advanceBackfill,
  advanceBackfillRest,
  configureBackfill,
  deleteBackfill,
  fetchEmailSources,
  getBackfill,
} from "@/data"
import type { BackfillStateData, BackfillWindowUnit } from "@/data"
import type { Source } from "@/types/domain"
import { SourceSelect } from "./fetch-control"

const UNITS: { v: BackfillWindowUnit; label: string }[] = [
  { v: "day", label: "Días" },
  { v: "week", label: "Semanas" },
  { v: "month", label: "Meses" },
]

type Busy = null | "window" | "rest" | "dry" | "config"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block">{label}</span>
      {children}
    </label>
  )
}

/** Una fila del history (ventana ya ejecutada), estilo RowResultView del fetch. */
function HistoryRow({ w }: { w: BackfillStateData["history"][number] }) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 text-xs">
      <span className="num text-muted-foreground">
        {w.start} → {w.end}
      </span>
      <span className="num ml-auto">
        <span className="text-status-ok">{formatInt(w.inserted)} nuevos</span>
        <span className="text-muted-foreground"> · {formatInt(w.duplicates)} ya</span>
        {w.filtered > 0 && (
          <span className="text-status-filtered"> · {formatInt(w.filtered)} filtr.</span>
        )}
      </span>
      {w.capHit && (
        <span
          className="text-status-review"
          title="tope alcanzado: la ventana pudo quedar truncada — achicala o subí el tope y re-corré"
        >
          ⚠ tope
        </span>
      )}
    </div>
  )
}

/**
 * Importación masiva (backfill) de UNA fuente de correo. La fuente la decide el padre
 * (`BackfillPanel` tiene su propio selector); `sourceId == null` → hint. El avance se persiste en
 * el server, así recargar retoma la frontera.
 */
export function BackfillSection({ sourceId }: { sourceId: number | null }) {
  const { data, loading, error, reload } = useAsync<BackfillStateData | null>(
    () => (sourceId == null ? Promise.resolve(null) : getBackfill(sourceId)),
    [sourceId],
  )
  // stale-while-revalidate: si `data` quedó de otra fuente, tratalo como nada hasta el refetch.
  const job = data && data.sourceId === sourceId ? data : null

  const [busy, setBusy] = useState<Busy>(null)
  const [reconfiguring, setReconfiguring] = useState(false)

  // Override del tamaño de la PRÓXIMA ventana (null → usar el default guardado del job).
  const [ovUnit, setOvUnit] = useState<BackfillWindowUnit | null>(null)
  const [ovCount, setOvCount] = useState<number | null>(null)
  const curUnit = ovUnit ?? job?.windowUnit ?? "month"
  const curCount = ovCount ?? job?.windowCount ?? 1

  // Formulario de alta / reconfiguración.
  const [formStart, setFormStart] = useState("")
  const [formEnd, setFormEnd] = useState("")
  const [formUnit, setFormUnit] = useState<BackfillWindowUnit>("month")
  const [formCount, setFormCount] = useState(1)
  const [formLimit, setFormLimit] = useState(2000)

  async function createJob() {
    if (sourceId == null || !formStart || !formEnd) return
    setBusy("config")
    try {
      await configureBackfill(sourceId, {
        rangeStart: formStart,
        rangeEnd: formEnd,
        windowUnit: formUnit,
        windowCount: formCount,
        perWindowLimit: formLimit,
      })
      setReconfiguring(false)
      setOvUnit(null)
      setOvCount(null)
      reload()
      toast.success("Backfill listo", { description: `${formStart} → ${formEnd}` })
    } catch (e) {
      toast.error("No se pudo crear el backfill", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  async function advance(kind: "window" | "rest" | "dry") {
    if (sourceId == null) return
    setBusy(kind)
    try {
      const res =
        kind === "rest"
          ? await advanceBackfillRest(sourceId)
          : await advanceBackfill(sourceId, {
              dryRun: kind === "dry",
              windowUnit: curUnit,
              windowCount: curCount,
            })
      if (kind !== "dry") {
        setOvUnit(null)
        setOvCount(null)
        reload()
      }
      if (res.window) {
        const w = res.window
        const verb = kind === "dry" ? "Dry-run" : "Ventana"
        toast.success(`${verb}: ${formatInt(w.inserted)} nuevos`, {
          description: `${w.start} → ${w.end} · ${formatInt(w.duplicates)} ya · ${formatInt(w.filtered)} filtr.`,
        })
      } else {
        toast.info("El backfill ya está completo")
      }
    } catch (e) {
      toast.error("La ventana falló", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  async function reset() {
    if (sourceId == null) return
    setBusy("config")
    try {
      await deleteBackfill(sourceId)
      setReconfiguring(false)
      reload()
    } catch (e) {
      toast.error("No se pudo resetear", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  function startReconfigure() {
    if (!job) return
    setFormStart(job.rangeStart)
    setFormEnd(job.rangeEnd)
    setFormUnit(job.windowUnit)
    setFormCount(job.windowCount)
    setFormLimit(job.perWindowLimit)
    setReconfiguring(true)
  }

  const disabled = busy !== null
  const datosHref = sourceId != null ? `/datos?source=${sourceId}` : "/datos"

  if (sourceId == null) {
    return (
      <p className="text-xs text-muted-foreground">
        Elegí una fuente de correo para importar su historial en ventanas.
      </p>
    )
  }
  if (error) return <ErrorState detail={error} onRetry={reload} />
  if (loading && !job) {
    return (
      <div className="flex items-center gap-2 px-1 py-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Cargando avance…
      </div>
    )
  }

  if (job && !reconfiguring) {
    // ---- Vista de progreso ----
    return (
      <div className="space-y-3">
        <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3">
          <div className="flex items-center justify-between text-xs">
            <span className="eyebrow">
              {job.rangeStart} → {job.rangeEnd}
            </span>
            <span className="num text-muted-foreground">
              {job.status === "done" ? "completo" : `frontera ${job.frontier}`}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className={cn(
                "h-full rounded-full transition-all",
                job.status === "done" ? "bg-status-ok" : "bg-brand",
              )}
              style={{ width: `${job.progressPct}%` }}
            />
          </div>
          <div className="num text-[11px] text-muted-foreground">
            {job.progressPct.toFixed(0)}% del rango
          </div>
        </div>

        {job.status === "done" ? (
          <div className="flex flex-wrap items-center gap-3">
            <span className="inline-flex items-center gap-1.5 text-sm text-status-ok">
              <CheckCircle2 className="size-4" /> Backfill completo
            </span>
            <Link
              to={datosHref}
              className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
            >
              Ver en datos <ArrowRight className="size-3.5" />
            </Link>
            <Button variant="ghost" size="sm" className="ml-auto" disabled={disabled} onClick={reset}>
              <RotateCcw className="size-3.5" /> Reiniciar
            </Button>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Tamaño de ventana">
                <Input
                  type="number"
                  min={1}
                  value={curCount}
                  onChange={(e) => setOvCount(Math.max(1, Number(e.target.value)))}
                  className="h-9"
                />
              </Field>
              <Field label="Unidad">
                <Select value={curUnit} onValueChange={(v) => setOvUnit(v as BackfillWindowUnit)}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {UNITS.map((u) => (
                      <SelectItem key={u.v} value={u.v} className="text-sm">
                        {u.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={disabled} onClick={() => advance("window")}>
                {busy === "window" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <Play className="size-3.5" />
                )}
                Procesar ventana
              </Button>
              <Button variant="secondary" size="sm" disabled={disabled} onClick={() => advance("rest")}>
                {busy === "rest" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <FastForward className="size-3.5" />
                )}
                Procesar el resto
              </Button>
              <Button variant="outline" size="sm" disabled={disabled} onClick={() => advance("dry")}>
                {busy === "dry" ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <FlaskConical className="size-3.5" />
                )}
                Dry-run
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto"
                disabled={disabled}
                onClick={startReconfigure}
              >
                Reconfigurar
              </Button>
            </div>
          </>
        )}

        {job.history.length > 0 && (
          <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
            {[...job.history].reverse().map((w, i) => (
              <HistoryRow key={`${w.start}-${w.end}-${i}`} w={w} />
            ))}
          </div>
        )}
      </div>
    )
  }

  // ---- Formulario de alta / reconfiguración ----
  return (
    <div className="space-y-3">
      {reconfiguring && (
        <p className="text-xs text-status-review">
          Reconfigurar reinicia la frontera y borra el historial de ventanas.
        </p>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Desde">
          <Input
            type="date"
            value={formStart}
            onChange={(e) => setFormStart(e.target.value)}
            className="h-9"
          />
        </Field>
        <Field label="Hasta (inclusive)">
          <Input
            type="date"
            value={formEnd}
            onChange={(e) => setFormEnd(e.target.value)}
            className="h-9"
          />
        </Field>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <Field label="Ventana">
          <Input
            type="number"
            min={1}
            value={formCount}
            onChange={(e) => setFormCount(Math.max(1, Number(e.target.value)))}
            className="h-9"
          />
        </Field>
        <Field label="Unidad">
          <Select value={formUnit} onValueChange={(v) => setFormUnit(v as BackfillWindowUnit)}>
            <SelectTrigger className="h-9 text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {UNITS.map((u) => (
                <SelectItem key={u.v} value={u.v} className="text-sm">
                  {u.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Tope por ventana">
          <Input
            type="number"
            min={1}
            value={formLimit}
            onChange={(e) => setFormLimit(Math.max(1, Number(e.target.value)))}
            className="h-9"
          />
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={disabled || !formStart || !formEnd} onClick={createJob}>
          {busy === "config" ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {reconfiguring ? "Reconfigurar" : "Crear backfill"}
        </Button>
        {reconfiguring && (
          <Button variant="ghost" size="sm" disabled={disabled} onClick={() => setReconfiguring(false)}>
            Cancelar
          </Button>
        )}
      </div>
    </div>
  )
}

/**
 * Panel propio de Importación masiva, con su selector de fuente de correo (antes vivía embebido en
 * "Traer a demanda" atado a "la primera imap tildada" — implícito y confuso). Colapsado por
 * defecto: es una herramienta puntual para histórico, no parte del flujo diario.
 */
export function BackfillPanel() {
  const { data: sources, loading, error, reload } = useAsync<Source[]>(() => fetchEmailSources(), [])
  const [sel, setSel] = useState("")
  const selId = sel ? Number(sel) : (sources?.[0]?.id ?? null)

  return (
    <CollapsiblePanel
      eyebrow="ingesta · histórico"
      title="Importación masiva (correo)"
      sub="Backfill por ventanas de fechas sobre una fuente de correo; no afecta el avance del modo incremental"
      bodyClassName="space-y-3"
    >
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading ? (
        <div className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando fuentes…
        </div>
      ) : !sources || sources.length === 0 ? (
        <EmptyState
          title="Sin fuentes de correo"
          hint="La importación masiva opera sobre correo (imap); creá una fuente para usarla."
        />
      ) : (
        <>
          <div className="max-w-sm">
            <Field label="Fuente de correo">
              <SourceSelect sources={sources} value={String(selId ?? "")} onChange={setSel} />
            </Field>
          </div>
          <BackfillSection sourceId={selId} />
        </>
      )}
    </CollapsiblePanel>
  )
}
