import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { FreshnessDot, RelativeTime } from "@/components/common/time"
import { JOB_LABEL } from "@/data"
import type { SourceHealthRow, WorkerLatestRow } from "@/data"
import type { WorkerJob } from "@/types/domain"

interface Cell {
  key: string
  label: string
  kind: "source" | "job"
  date: string | null
  running?: boolean
}

export function FreshnessGrid({
  sources,
  workers,
}: {
  sources: SourceHealthRow[]
  workers: WorkerLatestRow[]
}) {
  const cells: Cell[] = [
    ...sources.map((s): Cell => ({
      key: `s-${s.sourceId}`,
      label: s.alias || s.accountEmail || s.name,
      kind: "source",
      date: s.lastRun?.startedAt ?? null,
      running: s.lastRun?.status === "running",
    })),
    ...workers.map((w): Cell => ({
      key: `j-${w.job}`,
      label: JOB_LABEL[w.job as WorkerJob] ?? w.job,
      kind: "job",
      date: w.latest?.startedAt ?? null,
      running: w.latest?.status === "running" || w.isStale,
    })),
  ]

  return (
    <Panel>
      <PanelHeader
        eyebrow="Pipeline · frescura"
        title="¿Qué tan actual es lo que veo?"
        sub="Antigüedad de la última actividad por source y por worker — verde <6 h, ámbar <24 h, rojo ≥24 h"
      />
      <PanelBody>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {cells.map((c) => (
            <div
              key={c.key}
              className="flex items-center gap-2.5 rounded-md border border-border bg-background/40 px-3 py-2.5"
            >
              {c.date ? (
                <FreshnessDot date={c.date} pulse={c.running} />
              ) : (
                <span className="led text-status-filtered" style={{ width: 8, height: 8 }} />
              )}
              <div className="min-w-0">
                <div className="truncate text-xs font-medium">{c.label}</div>
                <div className="num text-[11px] text-muted-foreground">
                  {c.date ? <RelativeTime date={c.date} /> : "sin datos"}
                </div>
              </div>
            </div>
          ))}
        </div>
      </PanelBody>
    </Panel>
  )
}
