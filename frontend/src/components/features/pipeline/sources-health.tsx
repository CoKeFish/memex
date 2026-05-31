import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { Sparkline } from "@/components/common/stat"
import { FreshnessDot, RelativeTime } from "@/components/common/time"
import { formatInt, formatPct } from "@/lib/format"
import { ingestionLabel, ingestionTone } from "@/lib/status"
import { sourceHealth } from "@/data"
import type { SourceType } from "@/types/domain"

const TYPE_LABEL: Record<SourceType, string> = {
  imap: "Email",
  telegram: "Telegram",
  social: "Social",
  calendar: "Calendar",
  gateway: "Gateway",
}

export function SourcesHealth() {
  const items = sourceHealth()
  return (
    <Panel>
      <PanelHeader
        eyebrow="Pipeline · ingesta"
        title="Salud de las sources"
        sub="Última corrida, tasa de éxito y volumen por fuente (ingestion_runs)"
      />
      <PanelBody>
        <Stateful
          skeleton={<TableSkeleton rows={6} cols={4} />}
          empty={<EmptyState title="Sin sources configuradas" hint="Configurá tu primera fuente para empezar a ingerir." />}
          errorDetail="HTTP 500 — GET /sources falló"
        >
          <div className="grid gap-3 md:grid-cols-2">
            {items.map(({ source, lastRun, runs, successRate, totalInserted, totalFiltered }) => (
              <div key={source.id} className="rounded-lg border border-border bg-background/40 p-3.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    {lastRun ? <FreshnessDot date={lastRun.startedAt} pulse={lastRun.status === "running"} /> : null}
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{source.name}</div>
                      <div className="eyebrow mt-0.5">
                        {TYPE_LABEL[source.type]}
                        {!source.enabled && " · deshabilitada"}
                      </div>
                    </div>
                  </div>
                  {lastRun && <StatusBadge tone={ingestionTone(lastRun.status)} label={ingestionLabel(lastRun.status)} pulse={lastRun.status === "running"} />}
                </div>

                <div className="mt-3 grid grid-cols-3 gap-2 text-center">
                  <Metric label="Éxito" value={formatPct(successRate, 0)} tone={successRate >= 0.95 ? "ok" : successRate >= 0.8 ? "warn" : "bad"} />
                  <Metric label="Insertados" value={formatInt(totalInserted)} />
                  <Metric label="Filtrados" value={formatInt(totalFiltered)} />
                </div>

                <div className="mt-3 flex items-center justify-between gap-3">
                  <span className="text-xs text-muted-foreground">
                    {lastRun ? <>última <RelativeTime date={lastRun.startedAt} /></> : "sin corridas"}
                  </span>
                  <div className="w-24">
                    <Sparkline data={[...runs].reverse().map((r) => r.inserted)} stroke="var(--chart-2)" />
                  </div>
                </div>

                {lastRun?.status === "failed" && lastRun.errorMessage && (
                  <p className="mt-2 truncate font-mono text-[11px] text-status-error" title={lastRun.errorMessage}>
                    {lastRun.errorClass}: {lastRun.errorMessage}
                  </p>
                )}
              </div>
            ))}
          </div>
        </Stateful>
      </PanelBody>
    </Panel>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" | "bad" }) {
  const cls = tone === "ok" ? "text-status-ok" : tone === "warn" ? "text-status-review" : tone === "bad" ? "text-status-error" : "text-foreground"
  return (
    <div className="rounded-md bg-muted/40 py-1.5">
      <div className={`num text-sm font-semibold ${cls}`}>{value}</div>
      <div className="eyebrow mt-0.5">{label}</div>
    </div>
  )
}
