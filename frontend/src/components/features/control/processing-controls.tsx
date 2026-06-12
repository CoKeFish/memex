// Paneles de /procesamiento: Scheduler (automático) y Módulos de extracción. El panel manual
// «Qué procesar» vive en manual-run.tsx (y su lote en lot-control.tsx).

import { useState } from "react"
import { Loader2, Power } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
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
import {
  type BatchingPolicy,
  fetchModules,
  fetchScheduler,
  type ModuleRow,
  setModule,
  setScheduler,
} from "@/data"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
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
  relevance: "Relevancia (candidatos)",
  relevance_gate: "Gate de relevancia",
  relevance_rules: "Minería de reglas",
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
