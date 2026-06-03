import { useState } from "react"
import { Bot, CalendarClock, Hand, Loader2, Lock, MapPin, Repeat } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { RelativeTime } from "@/components/common/time"
import { VirtualList } from "@/components/common/virtual-list"
import { formatDate } from "@/lib/format"
import { recentWindow, upcoming } from "@/lib/calendar-window"
import { originChart, originLabel, type Tone } from "@/lib/status"
import {
  fetchCalendarConflicts,
  fetchCalendarProviderAccounts,
  fetchCalendarSyncRuns,
  fetchDedupDecisions,
  NOW,
} from "@/data"
import { useAsync } from "@/lib/use-async"
import type {
  CalendarConflict,
  CalendarOrigin,
  CalendarSyncRun,
  ConsolidatedEvent,
  DedupDecision,
  ProviderAccount,
} from "@/types/domain"

const todayStr = `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, "0")}-${String(NOW.getDate()).padStart(2, "0")}`

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
  const upcoming = events.filter((e) => e.startsOn >= todayStr).slice(0, 12)
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · agenda" title="Próximos eventos" sub="Capa consolidada (mod_calendar_consolidated)" />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading ? (
          <PanelLoader label="Cargando agenda…" />
        ) : upcoming.length === 0 ? (
          <EmptyState title="Sin eventos próximos" />
        ) : (
          <ul className="max-h-[420px] divide-y divide-border overflow-y-auto">
            {upcoming.map((e) => (
              <li key={e.id}>
                <button
                  type="button"
                  onClick={() => onSelect(e)}
                  className="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-accent/40"
                >
                  <div className="num w-12 shrink-0 text-center">
                    <div className="text-[11px] uppercase text-muted-foreground">{formatDate(e.startsOn).split(" ")[1]}</div>
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
            <div className="num mt-0.5 text-[11px] text-muted-foreground">{formatDate(dD.a.startsOn)}</div>
          </div>
          <StatusBadge tone={dec.tone} label={dec.label} />
        </button>
        {open && (
          <div className="space-y-1.5 bg-background/40 px-4 pb-3 pt-1">
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
            <span>{c.recurring ? `${formatDate(c.firstOn)} – ${formatDate(c.lastOn)}` : formatDate(c.a.startsOn)}</span>
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

const TOKEN_STATE: Record<ProviderAccount["tokenState"], { label: string; tone: Tone }> = {
  delta: { label: "delta", tone: "ok" },
  "full-resync": { label: "410", tone: "review" },
  never: { label: "nuevo", tone: "neutral" },
}

export function SyncPanel() {
  const accountsState = useAsync<ProviderAccount[]>(() => fetchCalendarProviderAccounts(), [])
  const runsState = useAsync<CalendarSyncRun[]>(() => fetchCalendarSyncRuns(), [])
  const providers = accountsState.data ?? []
  const runs = runsState.data ?? []
  const loading = (accountsState.loading && !accountsState.data) || (runsState.loading && !runsState.data)
  const error = accountsState.error ?? runsState.error
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · sync" title="Proveedores y sincronización" sub="Ingress/egress con Google (mod_calendar_sync_runs)" />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} />
        ) : loading ? (
          <PanelLoader label="Cargando sincronización…" />
        ) : providers.length === 0 && runs.length === 0 ? (
          <EmptyState title="Sin cuentas de proveedor" hint="Conectá una cuenta con memex-calendar-sync." />
        ) : (
          <>
            <div className="space-y-2">
              {providers.map((p) => {
                const ts = TOKEN_STATE[p.tokenState]
                return (
                  <div key={p.id} className="flex items-center justify-between gap-2 rounded-md border border-border bg-background/40 px-3 py-2">
                    <div className="text-sm">
                      <span className="font-medium capitalize">
                        {p.provider} · {p.accountLabel}
                      </span>
                      <span className="num ml-2 text-[11px] text-muted-foreground">
                        {p.lastSyncAt ? <RelativeTime date={p.lastSyncAt} /> : "sin sync"}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <StatusBadge tone={ts.tone} label={ts.label} />
                      {p.writeBack && <StatusBadge tone="ok" label="write-back" />}
                    </div>
                  </div>
                )
              })}
            </div>
            <div>
              <div className="eyebrow mb-1.5">corridas recientes</div>
              {runs.length === 0 ? (
                <p className="px-1 text-xs text-muted-foreground">Sin corridas de sync todavía.</p>
              ) : (
                <ul className="divide-y divide-border rounded-md border border-border">
                  {runs.map((r) => (
                    <li key={r.id} className="flex items-center justify-between gap-2 px-3 py-1.5 text-xs">
                      <span className="flex items-center gap-2">
                        <span className={cn("num rounded px-1 py-0.5 text-[10px]", r.direction === "ingress" ? "bg-origin-provider/15 text-origin-provider" : "bg-brand/15 text-brand")}>
                          {r.direction}
                        </span>
                        <span className="truncate">{r.account}</span>
                      </span>
                      <span className="num flex items-center gap-2 text-muted-foreground">
                        <span title="created/modified/deleted">+{r.created}/~{r.modified}/-{r.deleted}</span>
                        {r.errors > 0 && <span className="text-status-error">{r.errors} err</span>}
                        <RelativeTime date={r.startedAt} />
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
