import { useEffect, useState } from "react"
import { Check, FlaskConical, Loader2, Play, Power } from "lucide-react"
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
import { formatInt, formatPct } from "@/lib/format"
import { sourceFullLabel } from "@/lib/inbox-format"
import {
  type BatchingPolicy,
  dryRunProcessing,
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
  setSourceEnabled,
} from "@/data"
import type { Source } from "@/types/domain"

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
              {data.jobs.map((j) => (
                <li key={j.name} className="flex items-center justify-between gap-3 px-3 py-2">
                  <div className="flex items-center gap-2.5">
                    <Switch
                      checked={j.enabled}
                      disabled={busy}
                      onCheckedChange={(c) => toggleJob(j.name, c)}
                      aria-label={`Habilitar ${j.name}`}
                    />
                    <span className="text-sm font-medium">{j.name}</span>
                    <span className="num text-[11px] text-muted-foreground">{j.defaultInterval}</span>
                  </div>
                  <div className="num flex items-center gap-3 text-[11px] text-muted-foreground">
                    {j.latest?.finishedAt && (
                      <span>
                        última <RelativeTime date={j.latest.finishedAt} />
                      </span>
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
              ))}
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
                  <span className="text-muted-foreground">{k}</span> {String(v)}
                </span>
              ))
            )}
          </div>
        )
      })}
    </div>
  )
}

export function ManualRunPanel() {
  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const { data: runs, reload: reloadRuns } = useAsync(() => fetchProcessingRuns(5), [])

  const [stages, setStages] = useState<Set<ProcessingStage>>(new Set(["classify"]))
  const [sourceId, setSourceId] = useState<string>(ALL_SOURCES)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [limit, setLimit] = useState("")
  const [only, setOnly] = useState<string>("")
  const [force, setForce] = useState(false)
  const [dry, setDry] = useState<{ count: number; sampleIds: number[] } | null>(null)
  const [busy, setBusy] = useState<null | "dry" | "run">(null)

  const latest = runs?.[0]
  const running = latest?.status === "running" && !latest.isStale

  // Polling: mientras la última corrida siga 'running', re-consultá cada 2.5s.
  useEffect(() => {
    if (!running) return
    const t = setTimeout(reloadRuns, 2500)
    return () => clearTimeout(t)
  }, [running, runs, reloadRuns])

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

  const noStages = stages.size === 0
  const disabled = busy !== null || running || noStages

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · manual"
        title="Qué procesar"
        sub="Elegí etapas y acotá por fuente, fecha o cantidad; el dry-run cuenta sin gastar y Ejecutar corre en background"
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
          {noStages && <span className="text-xs text-status-review">Elegí al menos una etapa.</span>}
          {running && (
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

        {runs && runs.length > 0 && (
          <div className="space-y-2">
            <div className="eyebrow">Corridas recientes</div>
            {runs.map((run) => (
              <div key={run.id} className="rounded-md border border-border p-2.5">
                <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="num text-muted-foreground">#{run.id}</span>
                    <span className="num">{(run.runConfig.stages ?? []).join(" → ")}</span>
                    <span className="num text-muted-foreground">
                      {run.stats?.targets ?? run.runConfig.targets?.length ?? 0} obj.
                    </span>
                  </div>
                  {run.isStale ? (
                    <StatusBadge tone="review" label="colgado" />
                  ) : run.status === "running" ? (
                    <span className="flex items-center gap-1 text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" /> corriendo
                    </span>
                  ) : (
                    <StatusBadge tone={run.status === "ok" ? "ok" : "error"} label={run.status} />
                  )}
                </div>
                {run.error ? (
                  <div className="text-[11px] text-status-error">{run.error}</div>
                ) : (
                  <RunResults run={run} />
                )}
              </div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Fuentes (toggle de ingesta) ---
export function SourcesTogglePanel() {
  const { data, loading, error, reload } = useAsync<Source[]>(() => fetchSources(), [])
  // Overrides optimistas por id; el display deriva de `data` + estos cambios (sin seedear en effect).
  const [overrides, setOverrides] = useState<Record<number, boolean>>({})
  const [busy, setBusy] = useState<number | null>(null)

  async function toggle(id: number, on: boolean) {
    setOverrides((p) => ({ ...p, [id]: on })) // optimista
    setBusy(id)
    try {
      await setSourceEnabled(id, on)
    } catch (e) {
      setOverrides((p) => ({ ...p, [id]: !on })) // revert
      toast.error("No se pudo cambiar la fuente", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · fuentes"
        title="Fuentes"
        sub="Habilitar/deshabilitar la ingesta por fuente (sources.enabled)"
        right={<CapBadge level="existe" title="PATCH /sources/{id} — persiste en la DB" />}
      />
      <PanelBody className="space-y-1.5">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <LoadingRow label="Cargando fuentes…" />
        ) : !data || data.length === 0 ? (
          <div className="px-2 py-6 text-sm text-muted-foreground">No hay fuentes.</div>
        ) : (
          data.map((s) => (
            <div
              key={s.id}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
            >
              <div>
                <div className="text-sm font-medium">{s.name}</div>
                <div className="eyebrow">{s.type}</div>
              </div>
              <Switch
                checked={overrides[s.id] ?? s.enabled}
                disabled={busy === s.id}
                onCheckedChange={(c) => toggle(s.id, c)}
              />
            </div>
          ))
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Módulos de extracción (toggle + cobertura) ---
const POLICIES: BatchingPolicy[] = ["per_module", "grouped", "all"]

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
        sub="Habilitar + política de batching por módulo (module_settings) + cobertura real"
        right={<CapBadge level="existe" title="GET/PATCH /modules — persiste en la DB" />}
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
                    <SelectTrigger className="h-7 w-auto gap-1 text-[11px]">
                      <span className="text-muted-foreground">policy</span>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {POLICIES.map((p) => (
                        <SelectItem key={p} value={p} className="text-xs">
                          {p}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                    group_size
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
