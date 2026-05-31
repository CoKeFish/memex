import { Bot, CalendarClock, Hand, Lock, MapPin } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { EmptyState } from "@/components/common/data-state"
import { RelativeTime } from "@/components/common/time"
import { formatDate } from "@/lib/format"
import { originChart, originLabel, type Tone } from "@/lib/status"
import { getAccount, getCalendarConflicts, getCalendarEvents, getCalendarSyncRuns, getDedupDecisions, NOW } from "@/data"
import type { CalendarOrigin, ConsolidatedEvent, DedupDecision } from "@/types/domain"

const todayStr = `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, "0")}-${String(NOW.getDate()).padStart(2, "0")}`

function OriginDots({ origins }: { origins: CalendarOrigin[] }) {
  return (
    <span className="flex items-center gap-1">
      {origins.map((o) => (
        <span key={o} className="size-2 rounded-full" style={{ background: originChart[o] }} title={originLabel[o]} />
      ))}
    </span>
  )
}

export function Agenda({ onSelect }: { onSelect: (e: ConsolidatedEvent) => void }) {
  const events = getCalendarEvents()
    .filter((e) => e.startsOn >= todayStr)
    .slice(0, 12)
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · agenda" title="Próximos eventos" sub="Capa consolidada (mod_calendar_consolidated)" />
      <PanelBody className="p-0">
        {events.length === 0 ? (
          <EmptyState title="Sin eventos próximos" />
        ) : (
          <ul className="max-h-[420px] divide-y divide-border overflow-y-auto">
            {events.map((e) => (
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
  const rows = getDedupDecisions()
  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="calendario · dedup"
        title="Descartados y fusiones"
        sub="Pares duplicados y por qué se fusionaron o se mantuvieron — decisión automática (LLM) o manual"
      />
      <PanelBody className="p-0">
        <ul className="divide-y divide-border">
          {rows.map((dD) => {
            const dec = DECISION[dD.status]
            return (
              <li key={dD.id} className="px-4 py-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 text-sm">
                    <span className="font-medium">{dD.a.title}</span>
                    <span className="mx-1.5 text-muted-foreground">↔</span>
                    <span className="font-medium">{dD.b.title}</span>
                  </div>
                  <StatusBadge tone={dec.tone} label={dec.label} />
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2">
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
                <p className="mt-1 text-xs text-muted-foreground">
                  <span className="text-muted-foreground/70">{dD.reason}.</span> {dD.rationale ?? "Esperando decisión de la FASE 2 (LLM) o manual."}
                </p>
              </li>
            )
          })}
        </ul>
      </PanelBody>
    </Panel>
  )
}

const CONFLICT_TONE: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "pendiente", tone: "review" },
  resolved: { label: "resuelto", tone: "ok" },
  dismissed: { label: "descartado", tone: "neutral" },
}

export function ConflictsList() {
  const rows = getCalendarConflicts()
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · conflictos" title="Conflictos" sub="Dos eventos distintos de alta importancia que se solapan (nunca se fusionan)" />
      <PanelBody className="p-0">
        <ul className="divide-y divide-border">
          {rows.map((c) => {
            const t = CONFLICT_TONE[c.status]
            return (
              <li key={c.id} className="px-4 py-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2 text-sm">
                    <CalendarClock className="size-4 shrink-0 text-status-review" />
                    <span>
                      <span className="font-medium">{c.a.title}</span> <span className="text-muted-foreground">↔</span>{" "}
                      <span className="font-medium">{c.b.title}</span>
                    </span>
                  </div>
                  <StatusBadge tone={t.tone} label={t.label} />
                </div>
                <p className="num mt-1 text-xs text-muted-foreground">
                  {formatDate(c.a.startsOn)} · {c.reason}
                </p>
              </li>
            )
          })}
        </ul>
      </PanelBody>
    </Panel>
  )
}

export function SyncPanel() {
  const providers = getAccount().providers
  const runs = getCalendarSyncRuns()
  return (
    <Panel className="overflow-hidden">
      <PanelHeader eyebrow="calendario · sync" title="Proveedores y sincronización" sub="Ingress/egress con Google (mod_calendar_sync_runs)" />
      <PanelBody className="space-y-3">
        <div className="space-y-2">
          {providers.map((p) => (
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
                <StatusBadge tone={p.tokenState === "delta" ? "ok" : "review"} label={p.tokenState === "delta" ? "delta" : "410"} />
                {p.writeBack && <StatusBadge tone="ok" label="write-back" />}
              </div>
            </div>
          ))}
        </div>
        <div>
          <div className="eyebrow mb-1.5">corridas recientes</div>
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
        </div>
      </PanelBody>
    </Panel>
  )
}
