// /asuntos — lo que está en marcha (cursos, trámites, proyectos, eventos), cada uno con su estado
// vigente y qué sigue (tareas pendientes y la fecha más próxima). Filtros por estado (chips con
// contador), prioridad, tipo y búsqueda; cada fila abre su ventana (/asuntos/:id). Las herramientas
// del módulo (asignador LLM, curador, ajustes) quedan arriba a la derecha; el interruptor
// «Inspección» (compartido con la ventana del asunto) suma la columna de evidencia y los ids.

import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { BookOpen, Loader2, Settings2, Sparkles, Wand2 } from "lucide-react"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { formatUsd } from "@/lib/format"
import { useInspectMode } from "@/lib/inspect-mode"
import { dateParts, localIsoDate, relativeDay } from "@/lib/asunto-story"
import {
  ASUNTO_KIND_LABEL,
  ASUNTO_KINDS,
  ASUNTO_PRIORITIES,
  ASUNTO_PRIORITY_HINT,
  ASUNTO_PRIORITY_LABEL,
  ASUNTO_STATUS_LABEL,
  ASUNTO_STATUS_PLURAL,
  ASUNTO_STATUS_TONE,
  ASUNTO_STATUSES,
} from "@/lib/asuntos"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState, TableSkeleton } from "@/components/common/data-state"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  assignAsuntos,
  curateAsuntos,
  fetchAsuntos,
  fetchAsuntosSettings,
  patchAsuntosSettings,
  type Asunto,
  type AsuntoKind,
  type AsuntoPriority,
  type AsuntosSettings,
  type AsuntoStatus,
} from "@/data"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

function useDebouncedValue<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

// ---- Ajustes del módulo -----------------------------------------------------------------------

function NumberField({
  label,
  value,
  min,
  onChange,
}: {
  label: string
  value: number
  min: number
  onChange: (n: number) => void
}) {
  return (
    <label className="flex items-center justify-between gap-3 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <Input
        type="number"
        min={min}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-7 w-20 text-right text-xs"
      />
    </label>
  )
}

function SettingsForm() {
  const { data, error, loading } = useAsync(fetchAsuntosSettings, [])
  const [draft, setDraft] = useState<Partial<AsuntosSettings>>({})
  const [saving, setSaving] = useState(false)

  if (error) return <ErrorState detail={error} className="py-6" />
  if (loading && !data) {
    return (
      <div className="flex items-center gap-2 py-4 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" /> Cargando ajustes…
      </div>
    )
  }
  if (!data) return null

  const cur: AsuntosSettings = { ...data, ...draft }
  const dirty = Object.keys(draft).length > 0
  const set = <K extends keyof AsuntosSettings>(k: K, v: AsuntosSettings[K]): void =>
    setDraft((d) => ({ ...d, [k]: v }))

  async function save(): Promise<void> {
    setSaving(true)
    try {
      await patchAsuntosSettings(draft)
      setDraft({})
      toast.success("Ajustes guardados")
    } catch (e) {
      toast.error(`Ajustes: ${errMsg(e)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="eyebrow">ajustes del módulo</div>
      <label className="flex items-center justify-between gap-3 text-xs">
        <span>Módulo activo</span>
        <Switch checked={cur.enabled} onCheckedChange={(v) => set("enabled", v)} size="sm" />
      </label>
      <label className="flex items-center justify-between gap-3 text-xs">
        <span>
          Adhesión determinista
          <span className="block text-[11px] text-muted-foreground">hilo/evento sin LLM</span>
        </span>
        <Switch
          checked={cur.adhesionEnabled}
          onCheckedChange={(v) => set("adhesionEnabled", v)}
          size="sm"
        />
      </label>
      <NumberField label="Mensajes por ventana" value={cur.batchSize} min={1} onChange={(n) => set("batchSize", n)} />
      <NumberField label="Días sin señal → inactivo" value={cur.inactiveDays} min={1} onChange={(n) => set("inactiveDays", n)} />
      <NumberField label="Evidencia mínima para abrir" value={cur.minEvidenceOpen} min={1} onChange={(n) => set("minEvidenceOpen", n)} />
      <div className="flex justify-end gap-2 pt-1">
        <Button size="xs" variant="ghost" disabled={!dirty || saving} onClick={() => setDraft({})}>
          Descartar
        </Button>
        <Button size="xs" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? "Guardando…" : "Guardar"}
        </Button>
      </div>
    </div>
  )
}

// ---- Herramientas del módulo --------------------------------------------------------------------

function ActionsBar({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState<"assign" | "curate" | null>(null)

  async function assign(): Promise<void> {
    setBusy("assign")
    try {
      const r = await assignAsuntos()
      toast.success(
        `Asignador: ${r.assigned} asignados · ${r.created} creados · ${r.none} sin asunto · ${formatUsd(r.costUsd)}` +
          (r.errors > 0 ? ` · ${r.errors} errores` : ""),
      )
      onDone()
    } catch (e) {
      toast.error(`Asignador: ${errMsg(e)}`)
    } finally {
      setBusy(null)
    }
  }

  async function curate(): Promise<void> {
    setBusy("curate")
    try {
      const r = await curateAsuntos({ scope: "all" })
      toast.success(
        `Curador: ${r.items} ítems · fundió ${r.merged}, renombró ${r.renamed}, cerró ${r.closed}, reabrió ${r.reopened}, borró ${r.deleted}, relacionó ${r.related}` +
          (r.errors > 0 ? ` · ${r.errors} errores` : ""),
      )
      onDone()
    } catch (e) {
      toast.error(`Curador: ${errMsg(e)}`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        disabled={busy !== null}
        onClick={() => void assign()}
        title="Asignador (LLM): cuelga a su asunto los mensajes relevantes que todavía no tienen uno"
      >
        {busy === "assign" ? <Loader2 className="size-3.5 animate-spin" /> : <Wand2 className="size-3.5" />}
        {busy === "assign" ? "Asignando…" : "Asignar pendientes"}
      </Button>
      <Button
        size="sm"
        variant="outline"
        disabled={busy !== null}
        onClick={() => void curate()}
        title="Curador (LLM): funde duplicados, revisa candidatos y abiertos, propone relaciones"
      >
        {busy === "curate" ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
        {busy === "curate" ? "Curando…" : "Curar"}
      </Button>
      <Popover>
        <PopoverTrigger asChild>
          <Button size="sm" variant="ghost" aria-label="Ajustes">
            <Settings2 className="size-3.5" /> Ajustes
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-80">
          <SettingsForm />
        </PopoverContent>
      </Popover>
    </>
  )
}

// ---- Filas ------------------------------------------------------------------------------------------

/** Qué sigue en una celda: fecha más próxima (día grande, mes chico, rojo si ya pasó) y cuántas
 * pendientes hay. Sin tareas: guion. */
function QueSigue({ a, today }: { a: Asunto; today: string }) {
  if (a.tareasPendientes === 0 && !a.proximaFecha) {
    return <span className="text-xs text-muted-foreground/60">—</span>
  }
  const p = a.proximaFecha ? dateParts(a.proximaFecha) : null
  const late = !!a.proximaFecha && a.proximaFecha < today
  return (
    <div className="flex items-center gap-2.5">
      {p && a.proximaFecha && (
        <div
          className={cn("num shrink-0 leading-none", late ? "text-status-error" : "text-foreground")}
          title={`${late ? "venció" : "vence"} ${relativeDay(a.proximaFecha, today)}`}
        >
          <div className="text-[17px] font-semibold tracking-tight">{p.day}</div>
          <div className="mt-0.5 text-[10px] text-muted-foreground">{p.month}</div>
        </div>
      )}
      <div className="text-xs text-muted-foreground">
        {a.tareasPendientes > 0
          ? `${a.tareasPendientes} ${a.tareasPendientes === 1 ? "pendiente" : "pendientes"}`
          : "sin pendientes"}
        {p && (
          <div className={cn("text-[11px]", late ? "text-status-error" : "text-muted-foreground/80")}>
            {late ? "venció" : "vence"} {relativeDay(a.proximaFecha!, today)}
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Página -----------------------------------------------------------------------------------

export function AsuntosPage() {
  const [statuses, setStatuses] = useState<Set<AsuntoStatus>>(new Set(["abierto"]))
  const [kind, setKind] = useState<AsuntoKind | "all">("all")
  const [priorities, setPriorities] = useState<Set<AsuntoPriority>>(new Set())
  const [q, setQ] = useState("")
  const dq = useDebouncedValue(q.trim(), 300)
  const [version, setVersion] = useState(0)
  const bump = (): void => setVersion((v) => v + 1)
  const [inspect, setInspect] = useInspectMode()
  const today = localIsoDate()

  const statusKey = ASUNTO_STATUSES.filter((s) => statuses.has(s)).join(",")
  const priorityKey = ASUNTO_PRIORITIES.filter((p) => priorities.has(p)).join(",")
  const { data, error, loading, reload } = useAsync(
    () =>
      fetchAsuntos({
        statuses: ASUNTO_STATUSES.filter((s) => statuses.has(s)),
        kind: kind === "all" ? undefined : kind,
        priorities: ASUNTO_PRIORITIES.filter((p) => priorities.has(p)),
        q: dq || undefined,
      }),
    [statusKey, kind, priorityKey, dq, version],
  )

  function toggleStatus(s: AsuntoStatus): void {
    setStatuses((prev) => {
      const next = new Set(prev)
      if (next.has(s)) next.delete(s)
      else next.add(s)
      return next
    })
  }

  function togglePriority(p: AsuntoPriority): void {
    setPriorities((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }

  const items = data?.items ?? []
  const counts = data?.counts ?? {}
  const filtered = statuses.size > 0 || kind !== "all" || priorities.size > 0 || dq !== ""

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Dominios"
        title="Asuntos"
        description="Lo que está en marcha: cursos, trámites, proyectos, eventos. Cada asunto junta sus mensajes, tareas, datos y archivos."
        actions={<ActionsBar onDone={bump} />}
      />

      <div className="flex flex-wrap items-center gap-2 text-sm">
        {ASUNTO_STATUSES.map((s) => {
          const on = statuses.has(s)
          return (
            <button
              key={s}
              type="button"
              onClick={() => toggleStatus(s)}
              aria-pressed={on}
              className={cn(
                "rounded-full border px-3 py-1 text-[12px]",
                on ? "border-brand bg-brand text-background" : "border-input hover:bg-muted/40",
              )}
            >
              {ASUNTO_STATUS_PLURAL[s]} <span className="num opacity-70">{counts[s] ?? 0}</span>
            </button>
          )
        })}
        <span className="ml-2 text-xs text-muted-foreground">prioridad</span>
        {ASUNTO_PRIORITIES.map((p) => {
          const on = priorities.has(p)
          return (
            <button
              key={p}
              type="button"
              onClick={() => togglePriority(p)}
              aria-pressed={on}
              title={ASUNTO_PRIORITY_HINT[p]}
              className={cn(
                "rounded-full border px-3 py-1 text-[12px]",
                on ? "border-brand bg-brand text-background" : "border-input hover:bg-muted/40",
              )}
            >
              {ASUNTO_PRIORITY_LABEL[p]}
            </button>
          )
        })}
        <Select value={kind} onValueChange={(v) => setKind(v as AsuntoKind | "all")}>
          <SelectTrigger size="sm" className="w-40">
            <SelectValue placeholder="Tipo" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Todos los tipos</SelectItem>
            {ASUNTO_KINDS.map((k) => (
              <SelectItem key={k} value={k}>
                {ASUNTO_KIND_LABEL[k]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar asunto…"
          className="ml-auto h-8 w-56 text-sm"
          aria-label="Buscar asunto"
        />
        <label
          className="inline-flex cursor-pointer items-center gap-2 text-xs text-muted-foreground"
          title="Suma la evidencia (cantidad de mensajes), los alias y los ids"
        >
          <Switch size="sm" checked={inspect} onCheckedChange={setInspect} aria-label="Modo inspección" />
          Inspección
        </label>
      </div>

      <section className="overflow-hidden rounded-lg border border-border bg-card">
        <header className="flex items-baseline justify-between gap-3 border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">
            {loading && !data ? "Cargando…" : `${items.length} ${items.length === 1 ? "asunto" : "asuntos"}`}
          </h2>
          <p className="text-xs text-muted-foreground">Ordenados por última señal.</p>
        </header>
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <TableSkeleton rows={6} cols={5} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<BookOpen className="size-5" />}
            title="Sin asuntos"
            hint={
              filtered
                ? "Ningún asunto coincide con los filtros. Otro estado o tipo puede servir."
                : "Cuando el asignador procese mensajes relevantes, acá aparecen los hilos que infiere."
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="px-4 py-2 font-medium">Asunto</th>
                  <th className="px-2 py-2 font-medium">Estado</th>
                  <th className="px-2 py-2 font-medium">Qué sigue</th>
                  <th className="px-2 py-2 font-medium">En qué está</th>
                  <th className="px-2 py-2 font-medium">Última señal</th>
                  {inspect && <th className="px-2 py-2 text-right font-medium">Evidencia</th>}
                  {inspect && <th className="px-4 py-2 text-right font-medium">id</th>}
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.id} className="border-b border-border/60 last:border-0 hover:bg-muted/30">
                    <td className="max-w-[24rem] px-4 py-2.5">
                      <div className="flex items-start gap-2">
                        <div className="min-w-0">
                          <Link to={`/asuntos/${a.id}`} className="font-medium leading-snug hover:underline">
                            {a.name}
                          </Link>
                          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
                            <span>{ASUNTO_KIND_LABEL[a.kind] ?? a.kind}</span>
                            {a.priority === "alta" && (
                              <span
                                className="rounded-full border border-status-review/40 px-1.5 text-status-review"
                                title={a.priorityReason || ASUNTO_PRIORITY_HINT.alta}
                              >
                                prioridad alta
                              </span>
                            )}
                            {a.dueOn && <span className="num">fecha límite {a.dueOn}</span>}
                            {inspect && a.aliases.length > 0 && (
                              <span className="truncate" title={a.aliases.join(" · ")}>
                                también: {a.aliases.join(" · ")}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-2 py-2.5">
                      <StatusBadge
                        tone={ASUNTO_STATUS_TONE[a.status] ?? "neutral"}
                        label={ASUNTO_STATUS_LABEL[a.status] ?? a.status}
                      />
                    </td>
                    <td className="whitespace-nowrap px-2 py-2.5">
                      <QueSigue a={a} today={today} />
                    </td>
                    <td className="max-w-[26rem] px-2 py-2.5">
                      <span className="line-clamp-2 text-muted-foreground" title={a.stateText}>
                        {a.stateText || "sin estado"}
                      </span>
                    </td>
                    <td className="num whitespace-nowrap px-2 py-2.5 text-muted-foreground">
                      {a.lastSignalAt ? <RelativeTime date={a.lastSignalAt} /> : "sin señal"}
                    </td>
                    {inspect && <td className="num px-2 py-2.5 text-right">{a.evidenceCount}</td>}
                    {inspect && <td className="num px-4 py-2.5 text-right text-muted-foreground/70">#{a.id}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
