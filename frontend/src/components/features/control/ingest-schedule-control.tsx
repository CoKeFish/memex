import { useEffect, useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { ExternalLink, Loader2, Power, TriangleAlert } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Led, StatusBadge } from "@/components/common/led"
import { CapBadge } from "@/components/common/cap-badge"
import { RelativeTime } from "@/components/common/time"
import { ErrorState } from "@/components/common/data-state"
import { sourceMeta, sourceFullLabel } from "@/lib/inbox-format"
import {
  fetchIngestScheduler,
  fetchIngestionRuns,
  fetchSources,
  type IngestScheduleSource,
  PAID_API_TYPES,
  PULLABLE_SOURCE_TYPES,
  setIngestScheduler,
  setSourceEnabled,
  setSourceSchedule,
} from "@/data"
import type { IngestionRun, IngestionRunStatus, Source } from "@/types/domain"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

/** Adapta una fila del scheduler (o un id+config sueltos) a la forma `Source` que espera sourceMeta
 *  (solo lee type/name/config) — así el icono/etiqueta de proveedor es el mismo que en el resto. */
function asSource(s: IngestScheduleSource): Source {
  return {
    id: s.sourceId,
    name: s.name,
    type: s.type as Source["type"],
    enabled: s.enabled,
    createdAt: "",
    config: s.config,
    fetchModes: ["incremental"], // sourceMeta solo lee type/name/config; default seguro
  }
}

// ---- Intervalos (ISO-8601) y su etiqueta legible ---
const OFF = "__off__"
const INTERVAL_PRESETS: { v: string; label: string }[] = [
  { v: OFF, label: "Sin agendar" },
  { v: "PT15M", label: "Cada 15 min" },
  { v: "PT30M", label: "Cada 30 min" },
  { v: "PT1H", label: "Cada hora" },
  { v: "PT6H", label: "Cada 6 horas" },
  { v: "P1D", label: "Cada día" },
]
const PRESET_LABEL = new Map(INTERVAL_PRESETS.map((p) => [p.v, p.label]))

// ---- Origen de la corrida (trigger) → etiqueta + tono del pill ---
const ORIGIN: Record<string, { label: string; cls: string }> = {
  manual: { label: "manual", cls: "bg-brand/10 text-brand" },
  daemon: { label: "daemon", cls: "bg-status-ok/15 text-status-ok" },
  backfill: { label: "backfill", cls: "bg-muted text-muted-foreground" },
  agent: { label: "agente", cls: "bg-status-review/15 text-status-review" },
  cli: { label: "cli", cls: "bg-muted text-muted-foreground" },
  dashboard: { label: "manual", cls: "bg-brand/10 text-brand" }, // legado pre-0025
}
const ORIGIN_FILTERS = ["manual", "daemon", "backfill", "agent", "cli"]

function OriginBadge({ trigger }: { trigger: string }) {
  const o = ORIGIN[trigger] ?? { label: trigger, cls: "bg-muted text-muted-foreground" }
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium uppercase", o.cls)}>
      {o.label}
    </span>
  )
}

function statusTone(status: IngestionRunStatus): "ok" | "error" | "review" | "neutral" {
  if (status === "ok") return "ok"
  if (status === "failed") return "error"
  if (status === "aborted") return "review"
  return "neutral" // running
}

function LoadingRow({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

// ============================================================================
// Panel unificado: ingesta por fuente (master daemon + on/off + intervalo por fuente)
// ============================================================================
export function IngestSchedulerPanel() {
  const { data, loading, error, reload } = useAsync(() => fetchIngestScheduler(), [])
  // `busy` = "master" (toggle del daemon) o el id de la fuente que se está mutando.
  const [busy, setBusy] = useState<number | "master" | null>(null)

  async function run(key: number | "master", fn: () => Promise<unknown>, errLabel: string) {
    setBusy(key)
    try {
      await fn()
      reload()
    } catch (e) {
      toast.error(errLabel, { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="carga · por fuente"
        title="Ingesta por fuente"
        sub="Prendé/apagá cada fuente y elegí cada cuánto se trae; arriba, el daemon que dispara las agendadas (relee este estado cada ciclo)"
        right={
          <CapBadge
            level="existe"
            title="control en runtime vía DB (ingest_scheduler_settings + sources.enabled/fetch_schedule); el daemon debe estar desplegado (docker compose --profile ingest-scheduler up -d) para tomar efecto"
          />
        }
      />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <LoadingRow label="Cargando ingesta por fuente…" />
        ) : !data ? null : (
          <>
            {/* Master toggle del daemon (Power verde/gris + estado en texto). */}
            <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-background/40 p-3">
              <div className="flex items-center gap-2.5">
                <Power
                  className={cn(
                    "size-4",
                    data.daemonEnabled ? "text-status-ok" : "text-muted-foreground",
                  )}
                />
                <div>
                  <div className="text-sm font-medium">Ingesta automática</div>
                  <div className="text-xs text-muted-foreground">
                    {data.daemonEnabled
                      ? "armado — trae las fuentes agendadas en sus intervalos"
                      : "apagado — nada se trae solo; agendá abajo y prendé esto"}
                  </div>
                </div>
              </div>
              <Switch
                checked={data.daemonEnabled}
                disabled={busy === "master"}
                onCheckedChange={(c) =>
                  void run("master", () => setIngestScheduler(c), "No se pudo cambiar el daemon")
                }
                aria-label="Ingesta automática (daemon)"
              />
            </div>

            {/* Una fila por fuente: estado on/off (LED + atenuado) + intervalo. */}
            {data.sources.length === 0 ? (
              <div className="px-2 py-6 text-sm text-muted-foreground">No hay fuentes.</div>
            ) : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {data.sources.map((s) => {
                  const src = asSource(s)
                  const m = sourceMeta(src)
                  const Icon = m.icon
                  const schedulable = PULLABLE_SOURCE_TYPES.has(s.type)
                  const paid = PAID_API_TYPES.has(s.type)
                  const value = s.fetchSchedule ?? OFF
                  // ISO fuera de los presets → lo agregamos como opción para no perderlo.
                  const hasCustom = s.fetchSchedule !== null && !PRESET_LABEL.has(s.fetchSchedule)
                  const rowBusy = busy === s.sourceId
                  return (
                    <li
                      key={s.sourceId}
                      className={cn(
                        "flex flex-col gap-1.5 px-3 py-2.5 transition-opacity",
                        !s.enabled && "opacity-55",
                      )}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex min-w-0 items-center gap-2.5">
                          <Led tone={s.enabled ? "ok" : "neutral"} />
                          <Icon
                            className={cn(
                              "size-4 shrink-0",
                              s.enabled ? m.tone : "text-muted-foreground",
                            )}
                          />
                          <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate text-sm font-medium">
                                {sourceFullLabel(src)}
                              </span>
                              {paid && (
                                <TriangleAlert
                                  className="size-3 shrink-0 text-status-review"
                                  aria-label="API de paga (Apify)"
                                />
                              )}
                            </div>
                            <div className="eyebrow">{s.type}</div>
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-2.5">
                          {schedulable ? (
                            <Select
                              value={value}
                              disabled={rowBusy}
                              onValueChange={(v) =>
                                void run(
                                  s.sourceId,
                                  () => setSourceSchedule(s.sourceId, v === OFF ? null : v),
                                  "No se pudo cambiar el intervalo",
                                )
                              }
                            >
                              <SelectTrigger className="h-8 w-36 text-xs">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {INTERVAL_PRESETS.map((p) => (
                                  <SelectItem key={p.v} value={p.v} className="text-xs">
                                    {p.label}
                                  </SelectItem>
                                ))}
                                {hasCustom && (
                                  <SelectItem value={s.fetchSchedule!} className="text-xs">
                                    {s.fetchSchedule}
                                  </SelectItem>
                                )}
                              </SelectContent>
                            </Select>
                          ) : (
                            <span className="text-[11px] text-muted-foreground">no agendable</span>
                          )}
                          <Switch
                            checked={s.enabled}
                            disabled={rowBusy}
                            onCheckedChange={(c) =>
                              void run(
                                s.sourceId,
                                () => setSourceEnabled(s.sourceId, c),
                                "No se pudo cambiar la fuente",
                              )
                            }
                            aria-label={`Ingesta de ${sourceFullLabel(src)}`}
                          />
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-[26px] text-[11px] text-muted-foreground">
                        {s.fetchSchedule && !s.enabled && (
                          <span className="text-status-review">
                            apagada: no se trae aunque esté agendada
                          </span>
                        )}
                        {s.fetchSchedule && s.enabled && paid && (
                          <span className="flex items-center gap-1 text-status-review">
                            <TriangleAlert className="size-3" /> API de paga: gasta en cada corrida
                          </span>
                        )}
                        {s.latest && (
                          <span className="num flex items-center gap-1.5">
                            última <RelativeTime date={s.latest.startedAt} />
                            <StatusBadge
                              tone={s.latest.isStale ? "review" : statusTone(s.latest.status)}
                              label={s.latest.isStale ? "colgado" : s.latest.status}
                            />
                          </span>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </>
        )}
      </PanelBody>
    </Panel>
  )
}

// ============================================================================
// Historial de corridas de ingesta (icono + origen + link a /logs)
// ============================================================================
const POLL_MS = 4000

/** Chips compactos de resultado de una corrida (nuevos/ya/filtr/err). */
function RunStats({ run }: { run: IngestionRun }) {
  const chips: [string, number][] = [
    ["nuevos", run.inserted],
    ["ya", run.duplicates],
    ["filtr", run.filtered],
    ["err", run.errors],
  ]
  return (
    <div className="num flex flex-wrap items-center gap-1.5 text-[11px]">
      {chips.map(([k, v]) => (
        <span key={k} className="rounded bg-muted/60 px-1.5 py-0.5">
          <span className="text-muted-foreground">{k}</span> {v}
        </span>
      ))}
    </div>
  )
}

export function IngestRunsPanel() {
  const [origin, setOrigin] = useState<string>("all")
  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const { data, loading, error, reload } = useAsync(
    () => fetchIngestionRuns({ limit: 20, trigger: origin === "all" ? undefined : origin }),
    [origin],
  )

  // Mapa id→Source para el icono/etiqueta de proveedor (mismo sourceMeta del resto).
  const sourceById = useMemo(() => {
    const m = new Map<number, Source>()
    for (const s of sources ?? []) m.set(s.id, s)
    return m
  }, [sources])

  // Polling: mientras alguna corrida siga 'running' (y no colgada), re-consultá cada POLL_MS.
  const anyRunning = (data ?? []).some((r) => r.status === "running" && !r.isStale)
  useEffect(() => {
    if (!anyRunning) return
    const t = setTimeout(reload, POLL_MS)
    return () => clearTimeout(t)
  }, [anyRunning, data, reload])

  return (
    <Panel>
      <PanelHeader
        eyebrow="carga · historial"
        title="Corridas de ingesta"
        sub="Cada corrida con su origen (manual / daemon / backfill / agente) y sus contadores; abrí la traza completa en Logs"
        right={
          <Select value={origin} onValueChange={setOrigin}>
            <SelectTrigger className="h-7 w-auto gap-1 text-[11px]">
              <span className="text-muted-foreground">origen</span>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" className="text-xs">
                todos
              </SelectItem>
              {ORIGIN_FILTERS.map((o) => (
                <SelectItem key={o} value={o} className="text-xs">
                  {ORIGIN[o]?.label ?? o}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      <PanelBody className="space-y-2">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <LoadingRow label="Cargando corridas…" />
        ) : !data || data.length === 0 ? (
          <div className="px-2 py-6 text-sm text-muted-foreground">
            Sin corridas {origin !== "all" ? `de origen "${ORIGIN[origin]?.label ?? origin}"` : ""}.
          </div>
        ) : (
          data.map((run) => {
            const src = sourceById.get(run.sourceId)
            const m = src ? sourceMeta(src) : null
            const Icon = m?.icon
            return (
              <div key={run.id} className="rounded-md border border-border p-2.5">
                <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                  <div className="flex min-w-0 items-center gap-2">
                    <OriginBadge trigger={run.trigger} />
                    {Icon && <Icon className={cn("size-3.5 shrink-0", m?.tone)} />}
                    <span className="truncate font-medium" title={src ? sourceFullLabel(src) : undefined}>
                      {src ? sourceFullLabel(src) : `fuente #${run.sourceId}`}
                    </span>
                    <span className="num text-muted-foreground">
                      <RelativeTime date={run.startedAt} />
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {run.isStale ? (
                      <StatusBadge tone="review" label="colgado" />
                    ) : run.status === "running" ? (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <Loader2 className="size-3 animate-spin" /> corriendo
                      </span>
                    ) : (
                      <StatusBadge tone={statusTone(run.status)} label={run.status} />
                    )}
                    <Link
                      to={`/logs?run_id=${encodeURIComponent(run.id)}`}
                      className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
                      title="Ver la traza de esta corrida en Logs"
                    >
                      <ExternalLink className="size-3.5" /> logs
                    </Link>
                  </div>
                </div>
                {run.errorMessage ? (
                  <div className="text-[11px] text-status-error">
                    {run.errorClass}: {run.errorMessage}
                  </div>
                ) : (
                  <RunStats run={run} />
                )}
              </div>
            )
          })
        )}
      </PanelBody>
    </Panel>
  )
}
