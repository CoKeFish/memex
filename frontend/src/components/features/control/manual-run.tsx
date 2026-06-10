// Panel «Qué procesar» de /procesamiento: UN form de filtros (etapas, fuente agrupada por medio,
// caso especial, fechas, tope, force) y dos modos de correrlo en tabs — «De una» (una sola corrida
// con todo el filtro) y «Por ventanas» (congela el filtro como lote y se avanza en tandas mirando
// el costo, ver lot-control). El dry-run cuenta sin gastar y vive en ambos tabs; «Corridas
// recientes» (compartida entre modos) cierra el panel.

import { useEffect, useState } from "react"
import { Check, FlaskConical, Layers, Loader2, Play, TriangleAlert } from "lucide-react"
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
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { CapBadge } from "@/components/common/cap-badge"
import { formatInt, formatUsd, formatUsdFine } from "@/lib/format"
import { groupSourcesByKind, sourceFullLabel } from "@/lib/inbox-format"
import {
  createLot,
  dryRunProcessing,
  fetchLot,
  fetchProcessingRuns,
  fetchSources,
  fetchWindowDefaults,
  PROCESSING_ONLY,
  PROCESSING_STAGES,
  type ProcessingRun,
  type ProcessingStage,
  runProcessing,
  runRequestMatchesLot,
} from "@/data"
import type { Source } from "@/types/domain"
import { DefaultsEditor, LotSection } from "./lot-control"

const ALL_SOURCES = "__all__"

/** Modo del panel: una sola corrida con todo el filtro vs lote avanzado por ventanas. */
type RunMode = "once" | "windows"

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

/** Defaults de ventana por medio cuando NO hay lote (GET /processing/window-defaults). Con lote,
 * LotSection muestra los del lote — misma forma, otro origen. */
function WindowDefaultsBlock({ disabled }: { disabled: boolean }) {
  const { data, reload } = useAsync(() => fetchWindowDefaults(), [])
  if (!data) return null
  return <DefaultsEditor defaults={data} disabled={disabled} onChanged={reload} />
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
  // Tamaño de ventana al CREAR el lote ("" → null → el backend resuelve por medio).
  const [newLotSize, setNewLotSize] = useState("")
  // Tab elegido por el usuario; mientras no toca, sigue al lote: con lote activo abre en
  // «Por ventanas». Derivado a propósito (nada de setState en efectos — regla del compiler).
  const [modeOverride, setModeOverride] = useState<RunMode | null>(null)
  const mode: RunMode = modeOverride ?? (lot ? "windows" : "once")

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
      const state = await createLot(buildReq(), lot ? null : newLotSize ? Number(newLotSize) : null)
      toast.success(`Lote creado: ${formatInt(state.total)} mensajes`, {
        description: `ventana de ${state.windowSize} msj · ${state.stages.join(" → ")}`,
      })
      setDry(null)
      setNewLotSize("")
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

  // Piezas compartidas por ambos tabs (el filtro es el mismo; solo cambia cómo se corre).
  const dryButton = (
    <Button variant="outline" size="sm" disabled={disabled} onClick={onDryRun}>
      <Spinner when={busy === "dry"} fallback={<FlaskConical className="size-3.5" />} /> Dry-run
    </Button>
  )
  const hints = (
    <>
      {noStages && <span className="text-xs text-status-review">Elegí al menos una etapa.</span>}
      {anyRunning && (
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" /> corrida en curso…
        </span>
      )}
    </>
  )
  const dryBox = dry && (
    <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
      <span className="num text-base font-semibold text-foreground">{formatInt(dry.count)}</span>{" "}
      mensaje(s) caen bajo el filtro.
      {dry.sampleIds.length > 0 && (
        <div className="num mt-1 text-[11px] text-muted-foreground">
          ej: {dry.sampleIds.slice(0, 12).join(", ")}
          {dry.count > 12 && " …"}
        </div>
      )}
    </div>
  )
  // El form cambió respecto a la config congelada del lote: avisar que el lote NO lo sigue.
  const lotDiverges = lot != null && !runRequestMatchesLot(buildReq(), lot)
  const divergenceNotice = lotDiverges && (
    <p className="flex items-start gap-1.5 rounded-md border border-status-review/30 bg-status-review/5 px-3 py-2 text-xs text-status-review">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span>
        El form de arriba no coincide con la config congelada del lote — las ventanas siguen
        corriendo con la del lote. «Reconfigurar lote» la reemplaza (resetea frontera e historial).
      </span>
    </p>
  )

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · manual"
        title="Qué procesar"
        sub="Elegí etapas y acotá por fuente, fecha o cantidad; el dry-run cuenta sin gastar. «De una» corre todo el filtro en una sola corrida; «Por ventanas» lo congela como lote y lo avanzás en tandas mirando el costo"
        right={
          <CapBadge level="existe" title="corre in-process en el API (reprocess) + polling de progreso" />
        }
      />
      <PanelBody className="space-y-3">
        {/* NO usar <Field> acá: un <label> que envuelve botones le regala todo el texto del grupo
            al nombre accesible del primer botón. Grupo plano con su eyebrow como texto. */}
        <div role="group" aria-label="Etapas">
          <span className="eyebrow mb-1 block">Etapas</span>
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
        </div>

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
                {groupSourcesByKind(sources ?? []).map((g) => (
                  <SelectGroup key={g.kind}>
                    <SelectLabel>{g.label}</SelectLabel>
                    {g.sources.map((s) => (
                      <SelectItem key={s.id} value={String(s.id)} className="text-sm" title={s.name}>
                        {sourceFullLabel(s)}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Caso especial">
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
                  Ninguno — procesa normal
                </SelectItem>
                {PROCESSING_ONLY.map((o) => (
                  <SelectItem key={o.key} value={o.key} className="text-sm">
                    {o.label}
                    <span className="text-muted-foreground"> — {o.hint}</span>
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

        <Tabs value={mode} onValueChange={(v) => setModeOverride(v as RunMode)}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="once" className="text-xs">
              De una
            </TabsTrigger>
            <TabsTrigger value="windows" className="text-xs">
              Por ventanas
            </TabsTrigger>
          </TabsList>

          <TabsContent value="once" className="space-y-3 pt-1">
            <div className="flex flex-wrap items-center gap-2">
              {dryButton}
              <Button size="sm" disabled={disabled} onClick={onRun}>
                <Spinner when={busy === "run"} fallback={<Play className="size-3.5" />} /> Ejecutar
              </Button>
              {hints}
            </div>
            {dryBox}
          </TabsContent>

          <TabsContent value="windows" className="space-y-3 pt-1">
            <div className="flex flex-wrap items-center gap-2">
              {dryButton}
              {!lot && (
                <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  ventana
                  <Input
                    type="number"
                    min={1}
                    value={newLotSize}
                    placeholder="auto"
                    disabled={disabled}
                    onChange={(e) => setNewLotSize(e.target.value)}
                    className="h-8 w-20 text-xs"
                    title="mensajes por ventana del lote; vacío = automático según el medio de las fuentes (ver defaults abajo)"
                  />
                  msj
                </label>
              )}
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
                {lot ? "Reconfigurar lote" : "Crear lote"}
              </Button>
              {hints}
            </div>
            {dryBox}
            {divergenceNotice}
            {lot ? (
              <LotSection
                lot={lot}
                disabled={busy !== null || running}
                onChanged={() => {
                  reloadLot()
                  reloadRuns()
                }}
              />
            ) : (
              <WindowDefaultsBlock disabled={busy !== null || running} />
            )}
          </TabsContent>
        </Tabs>

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
