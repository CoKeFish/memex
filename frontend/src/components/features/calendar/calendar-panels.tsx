import { useState } from "react"
import { Link } from "react-router-dom"
import { ArrowUpRight, Bot, CalendarClock, Hand, Loader2, Lock, MapPin, RefreshCw, Repeat } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { RelativeTime } from "@/components/common/time"
import { VirtualList } from "@/components/common/virtual-list"
import { formatDateOnly } from "@/lib/format"
import { recentWindow, todayKey, upcoming } from "@/lib/calendar-window"
import { ApiError } from "@/lib/api"
import { originChart, originLabel, type Tone } from "@/lib/status"
import {
  fetchCalendarSyncHealth,
  fetchCalendarConflicts,
  fetchCalendarSyncRuns,
  fetchDedupDecisions,
  syncCalendarAccountNow,
} from "@/data"
import { useAsync } from "@/lib/use-async"
import type {
  CalendarAccountHealth,
  CalendarConflict,
  CalendarOrigin,
  CalendarSyncHealth,
  CalendarSyncRun,
  ConsolidatedEvent,
  DedupDecision,
} from "@/types/domain"

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

/** Pills de filtro segmentado (mismo look que metrics-filters). */
function SegmentedFilter<K extends string>({
  value,
  options,
  onChange,
}: {
  value: K
  options: { key: K; label: string }[]
  onChange: (k: K) => void
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          onClick={() => onChange(o.key)}
          className={cn(
            "rounded px-2 py-0.5 text-[11px] font-medium transition-colors",
            value === o.key
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

function OriginDots({ origins }: { origins: CalendarOrigin[] }) {
  return (
    <span className="flex items-center gap-1">
      {origins.map((o) => (
        <span key={o} className="size-2 rounded-full" style={{ background: originChart[o] }} title={originLabel[o]} />
      ))}
    </span>
  )
}

export function Agenda({
  events,
  onSelect,
  loading,
  error,
}: {
  events: ConsolidatedEvent[]
  onSelect: (e: ConsolidatedEvent) => void
  loading?: boolean
  error?: string | null
}) {
  // Orden CRONOLÓGICO antes de cortar: la API lista por id (orden de consolidación), no por
  // fecha — sin el sort, "próximos" eran 12 eventos arbitrarios. El «hoy» se calcula en render.
  const todayStr = todayKey()
  const next = events
    .filter((e) => e.startsOn >= todayStr)
    .sort(
      (a, b) =>
        a.startsOn.localeCompare(b.startsOn) ||
        (a.startTime ?? "").localeCompare(b.startTime ?? ""),
    )
    .slice(0, 12)
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · agenda" title="Próximos eventos" sub="Capa consolidada (mod_calendar_consolidated)" />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading ? (
          <PanelLoader label="Cargando agenda…" />
        ) : next.length === 0 ? (
          <EmptyState title="Sin eventos próximos" />
        ) : (
          <ul className="max-h-[420px] divide-y divide-border overflow-y-auto">
            {next.map((e) => (
              <li key={e.id}>
                <button
                  type="button"
                  onClick={() => onSelect(e)}
                  className="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-accent/40"
                >
                  <div className="num w-12 shrink-0 text-center">
                    <div className="text-[11px] uppercase text-muted-foreground">{formatDateOnly(e.startsOn).split(" ")[1]}</div>
                    <div className="text-base font-semibold leading-tight">{new Date(e.startsOn).getUTCDate()}</div>
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {e.protected && <Lock className="size-3 text-brand" />}
                      <span className="truncate text-sm font-medium">{e.title}</span>
                      {e.memberCount > 1 && <span className="eyebrow">·{e.memberCount} fuentes</span>}
                    </div>
                    <div className="num mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11px] text-muted-foreground">
                      <span>{e.startTime ? `${e.startTime}${e.endTime ? `–${e.endTime}` : ""}` : "todo el día"}</span>
                      {e.location && (
                        <span className="inline-flex items-center gap-0.5">
                          <MapPin className="size-3" /> {e.location}
                        </span>
                      )}
                      <OriginDots origins={e.origins} />
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

const DECISION: Record<DedupDecision["status"], { label: string; tone: Tone }> = {
  confirmed: { label: "fusionados", tone: "ok" },
  rejected: { label: "separados", tone: "filtered" },
  candidate: { label: "pendiente", tone: "review" },
}

export function DedupDecisions() {
  const { data, loading, error } = useAsync<DedupDecision[]>(() => fetchDedupDecisions(), [])
  const [filter, setFilter] = useState<"recent" | "all">("recent")
  const [openId, setOpenId] = useState<number | null>(null)
  const all = data ?? []
  // Ventana por la fecha del par (a.startsOn): últimos 5 + próximos 4.
  const shown = filter === "all" ? all : recentWindow(all, (d) => d.a.startsOn, 5, 4)

  const row = (dD: DedupDecision) => {
    const dec = DECISION[dD.status]
    const open = openId === dD.id
    return (
      <div className="border-b border-border">
        <button
          type="button"
          onClick={() => setOpenId(open ? null : dD.id)}
          className="flex w-full items-start justify-between gap-2 px-4 py-2.5 text-left hover:bg-accent/40"
        >
          <div className="min-w-0">
            <div className="truncate text-sm">
              <span className="font-medium">{dD.a.title}</span>
              <span className="mx-1.5 text-muted-foreground">↔</span>
              <span className="font-medium">{dD.b.title}</span>
            </div>
            <div className="num mt-0.5 text-[11px] text-muted-foreground">{formatDateOnly(dD.a.startsOn)}</div>
          </div>
          <StatusBadge tone={dec.tone} label={dec.label} />
        </button>
        {open && (
          <div className="space-y-2 bg-background/40 px-4 pb-3 pt-2">
            <div className="space-y-1.5">
              {[dD.a, dD.b].map((e, i) => (
                <div key={e.id} className="rounded border border-border/60 p-2">
                  <div className="flex items-start gap-1.5">
                    <span
                      className="mt-1 size-2 shrink-0 rounded-full"
                      style={{ background: originChart[e.origin] }}
                      title={originLabel[e.origin]}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium text-foreground">{e.title}</div>
                      <div className="num mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground">
                        <span>{formatDateOnly(e.startsOn)}</span>
                        {e.startTime && <span>{e.startTime}</span>}
                        {e.location && <span>· {e.location}</span>}
                        {e.provider && <span>· {e.provider}</span>}
                      </div>
                      {e.sourceInboxIds.length > 0 ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {e.sourceInboxIds.map((sid) => (
                            <Link
                              key={sid}
                              to={`/datos/${sid}`}
                              className="num inline-flex items-center gap-0.5 rounded bg-origin-inbox/10 px-1.5 py-0.5 text-[10px] text-origin-inbox hover:bg-origin-inbox/20"
                            >
                              inbox #{sid} <ArrowUpRight className="size-2.5" />
                            </Link>
                          ))}
                        </div>
                      ) : e.provider ? (
                        <div className="mt-1 text-[10px] text-muted-foreground">
                          evento del proveedor ({e.provider}) — sin mensaje de origen
                        </div>
                      ) : null}
                    </div>
                    <span className="eyebrow shrink-0">{i === 0 ? "A" : "B"}</span>
                  </div>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {dD.decidedBy === "llm" ? (
                <span className="inline-flex items-center gap-1 rounded border border-status-running/40 bg-status-running/10 px-1.5 py-0.5 text-[10px] font-medium text-status-running">
                  <Bot className="size-3" /> auto · LLM{dD.confidence != null ? ` ${Math.round(dD.confidence * 100)}%` : ""}
                </span>
              ) : dD.decidedBy === "manual" ? (
                <span className="inline-flex items-center gap-1 rounded border border-brand/40 bg-brand/10 px-1.5 py-0.5 text-[10px] font-medium text-brand">
                  <Hand className="size-3" /> manual
                </span>
              ) : (
                <span className="eyebrow">sin decidir</span>
              )}
              {dD.score != null && <span className="num text-[11px] text-muted-foreground">score {dD.score}</span>}
              {dD.decidedAt && (
                <span className="num text-[11px] text-muted-foreground">
                  <RelativeTime date={dD.decidedAt} />
                </span>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              <span className="text-muted-foreground/70">{dD.reason}.</span> {dD.rationale ?? "Esperando decisión de la FASE 2 (LLM) o manual."}
            </p>
          </div>
        )}
      </div>
    )
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="calendario · dedup"
        title="Descartados y fusiones"
        sub="Pares duplicados — clic en uno para ver la razón (decisión LLM o manual)"
        right={
          <SegmentedFilter
            value={filter}
            onChange={setFilter}
            options={[
              { key: "recent", label: "Recientes" },
              { key: "all", label: "Todos" },
            ]}
          />
        }
      />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando dedup…" />
        ) : all.length === 0 ? (
          <EmptyState title="Sin pares de dedup" hint="El dedup FASE 1 no marcó pares duplicados todavía." />
        ) : filter === "all" ? (
          <VirtualList items={shown} getKey={(d) => d.id} renderItem={row} />
        ) : (
          <div>
            {shown.map((d) => (
              <div key={d.id}>{row(d)}</div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

const CONFLICT_TONE: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "pendiente", tone: "review" },
  resolved: { label: "resuelto", tone: "ok" },
  dismissed: { label: "descartado", tone: "neutral" },
}

export function ConflictsList({ onSelect }: { onSelect: (c: CalendarConflict) => void }) {
  const { data, loading, error } = useAsync<CalendarConflict[]>(() => fetchCalendarConflicts(), [])
  const [filter, setFilter] = useState<"upcoming" | "all">("upcoming")
  const all = data ?? []
  // "Próximos": el grupo todavía tiene choques de hoy en adelante (lastOn >= hoy).
  const shown = filter === "all" ? all : upcoming(all, (c) => c.lastOn)

  const row = (c: CalendarConflict) => {
    const t = CONFLICT_TONE[c.status]
    return (
      <button
        type="button"
        onClick={() => onSelect(c)}
        className="flex w-full items-start justify-between gap-2 border-b border-border px-4 py-3 text-left hover:bg-accent/40"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm">
            <CalendarClock className="size-4 shrink-0 text-status-review" />
            <span className="truncate">
              <span className="font-medium">{c.a.title}</span> <span className="text-muted-foreground">↔</span>{" "}
              <span className="font-medium">{c.b.title}</span>
            </span>
          </div>
          <div className="num mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{c.recurring ? `${formatDateOnly(c.firstOn)} – ${formatDateOnly(c.lastOn)}` : formatDateOnly(c.a.startsOn)}</span>
            {c.recurring && (
              <span className="inline-flex items-center gap-0.5 rounded bg-origin-provider/15 px-1 py-0.5 text-[10px] font-medium text-origin-provider">
                <Repeat className="size-2.5" /> ×{c.instanceCount}
              </span>
            )}
          </div>
        </div>
        <StatusBadge tone={t.tone} label={t.label} />
      </button>
    )
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="calendario · conflictos"
        title="Conflictos"
        sub="Choques de horario — clic para ver el detalle e iluminarlo en el calendario"
        right={
          <SegmentedFilter
            value={filter}
            onChange={setFilter}
            options={[
              { key: "upcoming", label: "Próximos" },
              { key: "all", label: "Todos" },
            ]}
          />
        }
      />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando conflictos…" />
        ) : all.length === 0 ? (
          <EmptyState title="Sin conflictos" hint="No hay choques de alta importancia pendientes de revisión." />
        ) : shown.length === 0 ? (
          <EmptyState title="Sin conflictos próximos" hint="Cambiá a «Todos» para ver también los pasados." />
        ) : filter === "all" ? (
          <VirtualList items={shown} getKey={(c) => c.id} renderItem={row} />
        ) : (
          <div>
            {shown.map((c) => (
              <div key={c.id}>{row(c)}</div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

/** Edad en horas → texto en llano para el panel de sync. */
function ageStr(hours: number | null): string {
  if (hours == null) return "nunca corrió"
  if (hours < 1) return `hace ${Math.round(hours * 60)} min`
  if (hours < 48) return `hace ${Math.round(hours)} h`
  return `hace ${Math.round(hours / 24)} días`
}

const CURSOR_STATE: Record<CalendarAccountHealth["cursorState"], { label: string; tone: Tone; hint: string }> = {
  incremental: {
    label: "al día (incremental)",
    tone: "ok",
    hint: "Hay cursor delta: la próxima sincronización trae solo los cambios.",
  },
  full_resync_pendiente: {
    label: "hará una sync completa",
    tone: "review",
    hint: "El cursor venció o se perdió: la próxima sincronización vuelve a traer todo.",
  },
  sin_primera_sync: {
    label: "sin primera sync",
    tone: "neutral",
    hint: "Esta cuenta todavía no bajó nada de Google.",
  },
}

/** LED + frase que responde «¿está funcionando?» (overall lo decide el servidor). */
function overallLine(h: CalendarSyncHealth): { tone: Tone; text: string } {
  const ages = h.accounts
    .filter((a) => a.enabled && a.lastPullAgeHours != null)
    .map((a) => a.lastPullAgeHours as number)
  const age = ages.length > 0 ? ageStr(Math.min(...ages)) : null
  switch (h.overall) {
    case "ok":
      return { tone: "ok", text: `Funcionando: última actualización desde el proveedor ${age}.` }
    case "desactualizado":
      return { tone: "review", text: `Desactualizado: la última actualización fue ${age}.` }
    case "error":
      return { tone: "error", text: "La última sincronización falló — revisá las corridas." }
    case "nunca":
      return { tone: "neutral", text: "Nunca se sincronizó con el proveedor." }
    case "sin_cuentas":
      return { tone: "neutral", text: "Sin cuentas de proveedor conectadas." }
  }
}

function SyncNowButton({ accountId, onDone }: { accountId: number; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  async function run() {
    setBusy(true)
    try {
      const res = await syncCalendarAccountNow(accountId)
      toast.success("Sincronizado con el proveedor", {
        description: `+${res.created} nuevos · ~${res.modified} cambiados · −${res.deleted} borrados · ${res.unchanged} sin cambios`,
      })
      onDone()
    } catch (e) {
      toast.error("No se pudo sincronizar", {
        description: e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e),
      })
    } finally {
      setBusy(false)
    }
  }
  return (
    <Button variant="outline" size="sm" className="h-7 text-xs" disabled={busy} onClick={run}>
      {busy ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
      Sincronizar ahora
    </Button>
  )
}

export function SyncPanel({ onSynced }: { onSynced?: () => void }) {
  const [refresh, setRefresh] = useState(0)
  const healthState = useAsync<CalendarSyncHealth>(() => fetchCalendarSyncHealth(), [refresh])
  const runsState = useAsync<CalendarSyncRun[]>(() => fetchCalendarSyncRuns(), [refresh])
  const health = healthState.data
  const runs = runsState.data ?? []
  const loading = (healthState.loading && !health) || (runsState.loading && !runsState.data)
  const error = healthState.error ?? runsState.error
  const refreshAll = () => {
    setRefresh((r) => r + 1)
    onSynced?.()
  }
  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="calendario · sync"
        title="Sincronización con el proveedor"
        sub="¿Está funcionando? Estado por cuenta y corridas recientes"
      />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} />
        ) : loading || !health ? (
          <PanelLoader label="Cargando sincronización…" />
        ) : (
          <>
            {(() => {
              const o = overallLine(health)
              return (
                <div className="space-y-1.5 rounded-md border border-border bg-background/40 px-3 py-2">
                  <div className="flex items-center gap-2 text-sm">
                    <StatusBadge tone={o.tone} label={health.overall.replace("_", " ")} />
                    <span>{o.text}</span>
                  </div>
                  {!health.autoSyncActive && (
                    <p className="text-xs text-muted-foreground">
                      La sincronización automática está apagada — los datos solo se actualizan
                      cuando sincronizás a mano (acá o por CLI).
                    </p>
                  )}
                </div>
              )
            })()}
            {health.accounts.length === 0 ? (
              <EmptyState title="Sin cuentas de proveedor" hint="Conectá una cuenta con memex-calendar-sync add-account." />
            ) : (
              <div className="space-y-2">
                {health.accounts.map((a) => {
                  const cs = CURSOR_STATE[a.cursorState]
                  return (
                    <div key={a.accountId} className="space-y-1.5 rounded-md border border-border bg-background/40 px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0 text-sm">
                          <span className="font-medium capitalize">
                            {a.provider} · {a.accountLabel}
                          </span>
                          {!a.enabled && <span className="eyebrow ml-2">deshabilitada</span>}
                        </div>
                        <SyncNowButton accountId={a.accountId} onDone={refreshAll} />
                      </div>
                      <div className="num flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span>
                          bajada: {ageStr(a.lastPullAgeHours)}
                          {a.lastPullStatus === "error" && <span className="text-status-error"> — falló</span>}
                        </span>
                        <span title={cs.hint}>
                          <StatusBadge tone={cs.tone} label={cs.label} />
                        </span>
                        {a.writeBack && (
                          <span title="memex puede crear/editar/borrar eventos en esta cuenta al hacer push (write-back).">
                            <StatusBadge tone="ok" label="escribe en el proveedor" />
                          </span>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
            <div>
              <div className="eyebrow mb-1.5">corridas recientes</div>
              {runs.length === 0 ? (
                <p className="px-1 text-xs text-muted-foreground">Sin corridas de sync todavía.</p>
              ) : (
                <>
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {runs.map((r) => (
                      <li key={r.id} className="flex items-center justify-between gap-2 px-3 py-1.5 text-xs">
                        <span className="flex items-center gap-2">
                          <span className={cn("num rounded px-1 py-0.5 text-[10px]", r.direction === "ingress" ? "bg-origin-provider/15 text-origin-provider" : "bg-brand/15 text-brand")}>
                            {r.direction === "ingress" ? "proveedor → memex" : "memex → proveedor"}
                          </span>
                          <span className="truncate">{r.account}</span>
                        </span>
                        <span className="num flex items-center gap-2 text-muted-foreground">
                          <span>+{r.created} · ~{r.modified} · −{r.deleted}</span>
                          {r.errors > 0 && <span className="text-status-error">{r.errors} err</span>}
                          <RelativeTime date={r.startedAt} />
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1 px-1 text-[10px] text-muted-foreground">
                    + nuevos · ~ cambiados · − borrados (eventos de esa corrida)
                  </p>
                </>
              )}
            </div>
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
