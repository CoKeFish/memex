import { EmptyState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { Sparkline } from "@/components/common/stat"
import { FreshnessDot, RelativeTime } from "@/components/common/time"
import { formatInt, formatPct } from "@/lib/format"
import { ingestionLabel, ingestionTone } from "@/lib/status"
import type { SourceHealthRow } from "@/data"
import type { SourceType } from "@/types/domain"

const TYPE_LABEL: Record<SourceType, string> = {
  imap: "Email",
  telegram: "Telegram",
  social: "Social",
  calendar: "Calendar",
  gateway: "Gateway",
}

export function SourcesHealth({ items }: { items: SourceHealthRow[] }) {
  return (
    <Panel>
      <PanelHeader
        eyebrow="Pipeline · ingesta"
        title="Salud de las sources"
        sub="Última corrida, tasa de éxito y volumen por fuente (ingestion_runs)"
      />
      <PanelBody>
        {items.length === 0 ? (
          <EmptyState
            title="Sin sources configuradas"
            hint="Configurá tu primera fuente para empezar a ingerir."
          />
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {items.map((s) => (
              <div key={s.sourceId} className="rounded-lg border border-border bg-background/40 p-3.5">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    {s.lastRun ? (
                      <FreshnessDot date={s.lastRun.startedAt} pulse={s.lastRun.status === "running"} />
                    ) : null}
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{s.name}</div>
                      <div className="eyebrow mt-0.5">
                        {TYPE_LABEL[s.type]}
                        {!s.enabled && " · deshabilitada"}
                      </div>
                    </div>
                  </div>
                  {s.lastRun && (
                    <StatusBadge
                      tone={ingestionTone(s.lastRun.status)}
                      label={ingestionLabel(s.lastRun.status)}
                      pulse={s.lastRun.status === "running"}
                    />
                  )}
                </div>

                <div className="mt-3 grid grid-cols-3 gap-2 text-center">
                  <Metric
                    label="Éxito"
                    value={formatPct(s.successRate, 0)}
                    tone={s.successRate >= 0.95 ? "ok" : s.successRate >= 0.8 ? "warn" : "bad"}
                  />
                  <Metric label="Insertados" value={formatInt(s.totalInserted)} />
                  <Metric label="Filtrados" value={formatInt(s.totalFiltered)} />
                </div>

                <div className="mt-3 flex items-center justify-between gap-3">
                  <span className="text-xs text-muted-foreground">
                    {s.lastRun ? (
                      <>
                        última <RelativeTime date={s.lastRun.startedAt} />
                      </>
                    ) : (
                      "sin corridas"
                    )}
                  </span>
                  <div className="w-24">
                    <Sparkline data={s.recent} stroke="var(--chart-2)" />
                  </div>
                </div>

                {s.lastRun?.status === "failed" && s.lastRun.errorMessage && (
                  <p
                    className="mt-2 truncate font-mono text-[11px] text-status-error"
                    title={s.lastRun.errorMessage}
                  >
                    {s.lastRun.errorClass}: {s.lastRun.errorMessage}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" | "bad" }) {
  const cls =
    tone === "ok"
      ? "text-status-ok"
      : tone === "warn"
        ? "text-status-review"
        : tone === "bad"
          ? "text-status-error"
          : "text-foreground"
  return (
    <div className="rounded-md bg-muted/40 py-1.5">
      <div className={`num text-sm font-semibold ${cls}`}>{value}</div>
      <div className="eyebrow mt-0.5">{label}</div>
    </div>
  )
}
