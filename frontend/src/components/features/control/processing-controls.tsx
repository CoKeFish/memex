import { useEffect, useState } from "react"
import { Check, FlaskConical, Layers, Loader2, Play, Power } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { CapBadge } from "@/components/common/cap-badge"
import { RelativeTime } from "@/components/common/time"
import { ErrorState } from "@/components/common/data-state"
import { formatInt, formatIsoInterval, formatPct, formatUsd, formatUsdFine } from "@/lib/format"
import { sourceFullLabel } from "@/lib/inbox-format"
import {
  type BatchingPolicy,
  createLot,
  dryRunProcessing,
  fetchLot,
  fetchModules,
  fetchProcessingRuns,
  fetchScheduler,
  fetchSources,
  type ModuleRow,
  PROCESSING_ONLY,
  PROCESSING_STAGES,
  type ProcessingRun,
  type ProcessingStage,
  runProcessing,
  setModule,
  setScheduler,
} from "@/data"
import type { Source } from "@/types/domain"
import { LotSection } from "./lot-control"

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

function Spinner({ when, fallback }: { when: boolean; fallback: React.ReactNode }) {
  return when ? <Loader2 className="size-3.5 animate-spin" /> : <>{fallback}</>
}

function LoadingRow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

// ---- Scheduler (procesamiento automático) ---

//: Etiquetas en español de los jobs. El slug (lo que se grepea en logs y DB) queda visible al lado.
const JOB_LABELS: Record<string, string> = {
  classify: "Clasificación",
  summarize: "Resumen",
  extract: "Extracción",
  ocr: "OCR (adjuntos)",
  calendar: "Calendario",
  finance: "Finanzas",
  identidades: "Identidades",
  relevance: "Relevancia",
  graph: "Grafo",
  log_purge: "Purga de logs",
}

/** Costo de unas stats de corrida: `cost_usd` plano (reprocess) o `cost.total.cost_usd` (jobs). */
function statsCost(stats: Record<string, unknown> | null | undefined): number | null {
  if (!stats) return null
  if (typeof stats.cost_usd === "number") return stats.cost_usd
  const nested = (stats.cost as { total?: { cost_usd?: unknown } } | undefined)?.total?.cost_usd
  if (typeof nested === "number") return nested
  if (typeof nested === "string") {
    const n = Number(nested)
    return Number.isFinite(n) ? n : null
  }
  return null
}

export function SchedulerPanel() {
  const { data, loading, error, reload } = useAsync(() => fetchScheduler(), [])
  const [busy, setBusy] = useState(false)

  async function patch(fn: () => Promise<unknown>) {
    setBusy(true)
    try {
      await fn()
      reload()
    } catch (e) {
      toast.error("No se pudo cambiar el scheduler", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  function toggleJob(name: string, on: boolean) {
    const current = new Set(data?.enabledJobs ?? [])
    if (on) current.add(name)
    else current.delete(name)
    void patch(() => setScheduler({ enabledJobs: [...current].join(",") }))
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · automático"
        title="Scheduler"
        sub="Prende/apaga el daemon y elige qué jobs corren; el daemon relee este estado cada ciclo"
        right={
          <CapBadge
            level="existe"
            title="control en runtime vía DB (scheduler_settings); el daemon debe estar desplegado para tomar efecto"
          />
        }
      />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <LoadingRow label="Cargando scheduler…" />
        ) : !data ? null : (
          <>
            <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-background/40 p-3">
              <div className="flex items-center gap-2.5">
                <Power
                  className={cn(
                    "size-4",
                    data.daemonEnabled ? "text-status-ok" : "text-muted-foreground",
                  )}
                />
                <div>
                  <div className="text-sm font-medium">Procesamiento automático</div>
                  <div className="text-xs text-muted-foreground">
                    {data.daemonEnabled
                      ? "armado — corre los jobs habilitados en intervalos"
                      : "apagado — nada procesa solo; corré lo que necesites abajo"}
                  </div>
                </div>
              </div>
              <Switch
                checked={data.daemonEnabled}
                disabled={busy}
                onCheckedChange={(c) => void patch(() => setScheduler({ daemonEnabled: c }))}
              />
            </div>
            <ul className="divide-y divide-border rounded-md border border-border">
              {data.jobs.map((j) => {
                const cost = statsCost(j.latest?.stats)
                return (
                  <li key={j.name} className="flex items-center justify-between gap-3 px-3 py-2">
                    <div className="flex items-center gap-2.5">
                      <Switch
                        checked={j.enabled}
                        disabled={busy}
                        onCheckedChange={(c) => toggleJob(j.name, c)}
                        aria-label={`Habilitar ${JOB_LABELS[j.name] ?? j.name}`}
                      />
                      <span className="text-sm font-medium">{JOB_LABELS[j.name] ?? j.name}</span>
                      <span className="num text-[10px] text-muted-foreground/70">{j.name}</span>
                      <span
                        className="num text-[11px] text-muted-foreground"
                        title={j.defaultInterval}
                      >
                        {formatIsoInterval(j.defaultInterval)}
                      </span>
                    </div>
                    <div className="num flex items-center gap-3 text-[11px] text-muted-foreground">
                      {j.latest?.finishedAt && (
                        <span>
                          última <RelativeTime date={j.latest.finishedAt} />
                        </span>
                      )}
                      {cost != null && cost > 0 && (
                        <span title={formatUsdFine(cost)}>{formatUsd(cost)}</span>
                      )}
                      {j.isStale ? (
                        <StatusBadge tone="review" label="colgado" />
                      ) : j.latest ? (
                        <StatusBadge
                          tone={
                            j.latest.status === "ok"
                              ? "ok"
                              : j.latest.status === "error"
                                ? "error"
                                : "neutral"
                          }
                          label={j.latest.status}
                        />
                      ) : (
                        <StatusBadge tone="neutral" label="sin correr" />
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- "Qué procesar" (corrida por lote) ---
const ALL_SOURCES = "__all__"

/** Resumen por etapa del resultado de una corrida (ok=3, errors=1, …) en chips. */
function RunResults({ run }: { run: ProcessingRun }) {
  const results = run.stats?.results ?? {}
  const stages = Object.keys(results)
  if (!stages.length) return null
  return (
    <div className="space-y-1.5">
      {stages.map((stage) => {
        const r = results[stage]
        const isErr = typeof r?.error === "string"
        return (
          <div key={stage} className="flex flex-wrap items-center gap-1.5 text-[11px]">
            <span className="font-medium">{stage}</span>
            {isErr ? (
              <span className="text-status-error">error: {String(r.error)}</span>
            ) : (
              Object.entries(r ?? {}).map(([k, v]) => (
                <span key={k} className="num rounded bg-muted/60 px-1.5 py-0.5">
                  {k === "cost_usd" ? (
                    <span title={formatUsdFine(Number(v))}>
                      <span className="text-muted-foreground">costo</span> {formatUsd(Number(v))}
                    </span>
                  ) : (
                    <>
                      <span className="text-muted-foreground">{k}</span> {String(v)}
                    </>
                  )}
                </span>
              ))
            )}
          </div>
        )
      })}
    </div>
  )
}

/** Costo total de una corrida: `stats.cost_usd` (nuevo) o la suma de etapas (corridas viejas). */
function runTotalCost(run: ProcessingRun): number | null {
  const direct = (run.stats as Record<string, unknown>).cost_usd
  if (typeof direct === "number") return direct
  const results = run.stats?.results
  if (!results) return null
  let sum = 0
  let seen = false
  for (const r of Object.values(results)) {
    const v = r?.cost_usd
    if (typeof v === "number") {
      sum += v
      seen = true
    }
  }
  return seen ? sum : null
}

/** Marca de origen de una corrida disparada por el lote (run_config.lot). */
function lotBadge(run: ProcessingRun): string | null {
  const lot = (run.runConfig as { lot?: { mode?: string } }).lot
  if (!lot) return null
  return lot.mode === "rest" ? "lote · resto" : "lote · ventana"
}

export function ManualRunPanel() {
  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const { data: runs, reload: reloadRuns } = useAsync(() => fetchProcessingRuns(5), [])
  const { data: lot, reload: reloadLot } = useAsync(() => fetchLot(), [])

  const [stages, setStages] = useState<Set<ProcessingStage>>(new Set(["classify"]))
  const [sourceId, setSourceId] = useState<string>(ALL_SOURCES)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [limit, setLimit] = useState("")
  const [only, setOnly] = useState<string>("")
  const [force, setForce] = useState(false)
  const [dry, setDry] = useState<{ count: number; sampleIds: number[] } | null>(null)
  const [busy, setBusy] = useState<null | "dry" | "run" | "lot">(null)

  const latest = runs?.[0]
  const running = latest?.status === "running" && !latest.isStale
  const anyRunning = running || (lot?.busy ?? false)

  // Polling: mientras haya una corrida 'running' (manual o ventana del lote), re-consultá cada
  // 2.5s — corridas Y lote, así la frontera/historial avanzan en vivo.
  useEffect(() => {
    if (!anyRunning) return
    const t = setTimeout(() => {
      reloadRuns()
      reloadLot()
    }, 2500)
    return () => clearTimeout(t)
  }, [anyRunning, runs, lot, reloadRuns, reloadLot])

  function toggleStage(s: ProcessingStage) {
    setStages((prev) => {
      const next = new Set(prev)
      if (next.has(s)) next.delete(s)
      else next.add(s)
      return next
    })
    setDry(null)
  }

  function buildReq() {
    return {
      stages: [...stages],
      sourceId: sourceId === ALL_SOURCES ? null : Number(sourceId),
      since: since || null,
      until: until || null,
      limit: limit ? Number(limit) : null,
      only: (only || null) as "unstored-attachments" | "errored" | null,
      force,
    }
  }

  async function onDryRun() {
    setBusy("dry")
    try {
      const r = await dryRunProcessing(buildReq())
      setDry({ count: r.count, sampleIds: r.sampleIds })
    } catch (e) {
      toast.error("Dry-run falló", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  async function onRun() {
    setBusy("run")
    try {
      const r = await runProcessing(buildReq())
      if (r.status === "empty") {
        toast.info("Sin objetivos para ese filtro", { description: "Nada que procesar." })
      } else {
        toast.success(`Corrida encolada: ${r.count} ${r.count === 1 ? "mensaje" : "mensajes"}`, {
          description: `etapas: ${r.stages.join(" → ")}`,
        })
        setDry(null)
        reloadRuns()
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.warning("Ya hay una corrida en curso", { description: "Esperá a que termine." })
      } else {
        toast.error("No se pudo encolar la corrida", { description: errMsg(e) })
      }
    } finally {
      setBusy(null)
    }
  }

  async function onCreateLot() {
    setBusy("lot")
    try {
      const state = await createLot(buildReq())
      toast.success(`Lote creado: ${formatInt(state.total)} mensajes`, {
        description: `ventana de ${state.windowSize} msj · ${state.stages.join(" → ")}`,
      })
      setDry(null)
      reloadLot()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        toast.warning("Ya hay una corrida en curso", { description: "Esperá a que termine." })
      } else {
        toast.error("No se pudo crear el lote", { description: errMsg(e) })
      }
    } finally {
      setBusy(null)
    }
  }

  const noStages = stages.size === 0
  const disabled = busy !== null || anyRunning || noStages

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · manual"
        title="Qué procesar"
        sub="Elegí etapas y acotá por fuente, fecha o cantidad; el dry-run cuenta sin gastar. Ejecutar corre todo de una; para un backlog grande creá un lote y avanzalo por ventanas mirando el costo"
        right={
          <CapBadge level="existe" title="corre in-process en el API (reprocess) + polling de progreso" />
        }
      />
      <PanelBody className="space-y-3">
        <Field label="Etapas">
          <div className="flex flex-wrap gap-2">
            {PROCESSING_STAGES.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => toggleStage(s.key)}
                aria-pressed={stages.has(s.key)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors",
                  stages.has(s.key)
                    ? "border-brand bg-brand/10 text-foreground"
                    : "border-border text-muted-foreground hover:bg-muted/40",
                )}
              >
                <span
                  className={cn(
                    "flex size-3.5 items-center justify-center rounded-[4px] border",
                    stages.has(s.key) ? "border-brand bg-brand text-background" : "border-input",
                  )}
                >
                  {stages.has(s.key) && <Check className="size-3" />}
                </span>
                {s.label}
                {s.llm && <span className="text-[10px] text-status-review">LLM</span>}
              </button>
            ))}
          </div>
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Fuente">
            <Select
              value={sourceId}
              onValueChange={(v) => {
                setSourceId(v)
                setDry(null)
              }}
            >
              <SelectTrigger className="h-9 text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_SOURCES} className="text-sm">
                  Todas las fuentes
                </SelectItem>
                {(sources ?? []).map((s) => (
                  <SelectItem key={s.id} value={String(s.id)} className="text-sm" title={s.name}>
                    {sourceFullLabel(s)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Solo (opcional)">
            <Select
              value={only || "__none__"}
              onValueChange={(v) => {
                setOnly(v === "__none__" ? "" : v)
                setDry(null)
              }}
            >
              <SelectTrigger className="h-9 text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__" className="text-sm">
                  Sin filtro
                </SelectItem>
                {PROCESSING_ONLY.map((o) => (
                  <SelectItem key={o.key} value={o.key} className="text-sm">
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Desde">
            <Input
              type="date"
              value={since}
              onChange={(e) => {
                setSince(e.target.value)
                setDry(null)
              }}
              className="h-9"
            />
          </Field>
          <Field label="Hasta">
            <Input
              type="date"
              value={until}
              onChange={(e) => {
                setUntil(e.target.value)
                setDry(null)
              }}
              className="h-9"
            />
          </Field>
          <Field label="Cantidad (límite)">
            <Input
              type="number"
              min={1}
              value={limit}
              placeholder="sin tope"
              onChange={(e) => {
                setLimit(e.target.value)
                setDry(null)
              }}
              className="h-9"
            />
          </Field>
        </div>

        <label className="flex items-center gap-2.5">
          <Switch
            checked={force}
            onCheckedChange={(c) => {
              setForce(c)
              setDry(null)
            }}
          />
          <span className="text-sm">
            Forzar reproceso
            <span className="ml-1 text-xs text-muted-foreground">(re-hace lo ya procesado)</span>
          </span>
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={disabled} onClick={onDryRun}>
            <Spinner when={busy === "dry"} fallback={<FlaskConical className="size-3.5" />} /> Dry-run
          </Button>
          <Button size="sm" disabled={disabled} onClick={onRun}>
            <Spinner when={busy === "run"} fallback={<Play className="size-3.5" />} /> Ejecutar
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={disabled}
            onClick={onCreateLot}
            title={
              lot
                ? "reemplaza el lote actual con estos filtros (resetea frontera e historial)"
                : "congela estos filtros como un lote y avanzalo por ventanas"
            }
          >
            <Spinner when={busy === "lot"} fallback={<Layers className="size-3.5" />} />
            {lot ? "Reconfigurar lote" : "Crear lote por ventanas"}
          </Button>
          {noStages && <span className="text-xs text-status-review">Elegí al menos una etapa.</span>}
          {anyRunning && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" /> corrida en curso…
            </span>
          )}
        </div>

        {dry && (
          <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
            <span className="num text-base font-semibold text-foreground">
              {formatInt(dry.count)}
            </span>{" "}
            mensaje(s) caen bajo el filtro.
            {dry.sampleIds.length > 0 && (
              <div className="num mt-1 text-[11px] text-muted-foreground">
                ej: {dry.sampleIds.slice(0, 12).join(", ")}
                {dry.count > 12 && " …"}
              </div>
            )}
          </div>
        )}

        {lot && (
          <LotSection
            lot={lot}
            disabled={busy !== null || running}
            onChanged={() => {
              reloadLot()
              reloadRuns()
            }}
          />
        )}

        {runs && runs.length > 0 && (
          <div className="space-y-2">
            <div className="eyebrow">Corridas recientes</div>
            {runs.map((run) => {
              const cost = runTotalCost(run)
              const badge = lotBadge(run)
              return (
                <div key={run.id} className="rounded-md border border-border p-2.5">
                  <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="num text-muted-foreground">#{run.id}</span>
                      <span className="num">{(run.runConfig.stages ?? []).join(" → ")}</span>
                      {badge && (
                        <span className="rounded bg-muted/60 px-1.5 py-0.5 text-[10px]">
                          {badge}
                        </span>
                      )}
                      <span className="num text-muted-foreground">
                        {run.stats?.targets ?? run.runConfig.targets?.length ?? 0} obj.
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      {cost != null && cost > 0 && (
                        <span className="num text-[11px]" title={formatUsdFine(cost)}>
                          {formatUsd(cost)}
                        </span>
                      )}
                      {run.isStale ? (
                        <StatusBadge tone="review" label="colgado" />
                      ) : run.status === "running" ? (
                        <span className="flex items-center gap-1 text-muted-foreground">
                          <Loader2 className="size-3 animate-spin" /> corriendo
                        </span>
                      ) : (
                        <StatusBadge
                          tone={run.status === "ok" ? "ok" : "error"}
                          label={run.status}
                        />
                      )}
                    </div>
                  </div>
                  {run.error ? (
                    <div className="text-[11px] text-status-error">{run.error}</div>
                  ) : (
                    <RunResults run={run} />
                  )}
                </div>
              )
            })}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Módulos de extracción (toggle + cobertura) ---

//: Políticas de batching con label en español + explicación (el value es lo que viaja a la DB).
const POLICIES: { value: BatchingPolicy; label: string; hint: string }[] = [
  {
    value: "grouped",
    label: "agrupados",
    hint: "los módulos elegidos comparten UNA llamada LLM por ventana (se parte solo si superan el tope) — lo más barato: cada mensaje viaja una sola vez",
  },
  {
    value: "per_module",
    label: "separados",
    hint: "una llamada LLM por módulo — el mismo mensaje viaja N veces (lo más caro, útil para aislar un módulo)",
  },
  {
    value: "all",
    label: "todos juntos",
    hint: "una sola llamada con todos los módulos, sin tope",
  },
]

export function ModulesTogglePanel() {
  const { data, loading, error, reload } = useAsync<ModuleRow[]>(() => fetchModules(), [])
  const [busy, setBusy] = useState<string | null>(null)

  async function patch(slug: string, fn: () => Promise<unknown>) {
    setBusy(slug)
    try {
      await fn()
      reload()
    } catch (e) {
      toast.error("No se pudo cambiar el módulo", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · módulos"
        title="Módulos de extracción"
        sub="Habilitá cada módulo y elegí cómo comparten la llamada LLM al extraer (agrupados = el mensaje viaja una vez). La barra es cobertura real: cuántos mensajes elegibles ya pasaron por cada módulo"
        right={<CapBadge level="existe" title="GET/PATCH /modules — persiste en module_settings" />}
      />
      <PanelBody className="space-y-2">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <LoadingRow label="Cargando módulos…" />
        ) : !data || data.length === 0 ? (
          <div className="px-2 py-6 text-sm text-muted-foreground">No hay módulos.</div>
        ) : (
          data.map((m) => {
            const pct = m.total ? m.processed / m.total : 0
            return (
              <div key={m.slug} className="rounded-md border border-border p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{m.label}</span>
                  <Switch
                    checked={m.enabled}
                    disabled={busy === m.slug}
                    onCheckedChange={(c) =>
                      void patch(m.slug, () => setModule(m.slug, { enabled: c }))
                    }
                  />
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Select
                    value={m.batchingPolicy}
                    onValueChange={(v) =>
                      void patch(m.slug, () =>
                        setModule(m.slug, { batchingPolicy: v as BatchingPolicy }),
                      )
                    }
                  >
                    <SelectTrigger
                      className="h-7 w-auto gap-1 text-[11px]"
                      title={POLICIES.find((p) => p.value === m.batchingPolicy)?.hint}
                    >
                      <span className="text-muted-foreground">batching</span>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {POLICIES.map((p) => (
                        <SelectItem key={p.value} value={p.value} className="text-xs" title={p.hint}>
                          {p.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <label
                    className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
                    title="módulos por llamada agrupada (solo aplica con batching agrupados)"
                  >
                    tope del grupo
                    <Input
                      type="number"
                      min={1}
                      defaultValue={m.groupSize}
                      disabled={busy === m.slug}
                      onBlur={(e) => {
                        const v = Number(e.target.value)
                        if (v >= 1 && v !== m.groupSize) {
                          void patch(m.slug, () => setModule(m.slug, { groupSize: v }))
                        }
                      }}
                      className="h-7 w-16 text-xs"
                    />
                  </label>
                </div>
                <div className="mt-2">
                  <div className="mb-0.5 flex justify-between text-[11px] text-muted-foreground">
                    <span>
                      cobertura · {formatInt(m.processed)}/{formatInt(m.total)}
                      {m.pending > 0 && <span> · {formatInt(m.pending)} pend.</span>}
                    </span>
                    <span className="num">{formatPct(pct, 0)}</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-brand"
                      style={{ width: `${pct * 100}%` }}
                    />
                  </div>
                </div>
              </div>
            )
          })
        )}
      </PanelBody>
    </Panel>
  )
}
