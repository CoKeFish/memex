import { TriangleAlert } from "lucide-react"
import { cn } from "@/lib/utils"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatDurationMs } from "@/lib/format"
import { workerLabel, workerTone } from "@/lib/status"
import { JOB_LABEL, workerLatest } from "@/data"
import type { WorkerRun } from "@/types/domain"

function flattenStats(stats: WorkerRun["stats"]): { k: string; v: number }[] {
  const out: { k: string; v: number }[] = []
  for (const [k, v] of Object.entries(stats)) {
    if (typeof v === "number") out.push({ k, v })
    else for (const [sk, sv] of Object.entries(v)) out.push({ k: `${k}.${sk}`, v: sv })
  }
  return out
}

export function WorkersBoard() {
  const items = workerLatest()
  return (
    <Panel>
      <PanelHeader
        eyebrow="Pipeline · workers"
        title="Estado de los workers"
        sub="Última corrida por job (worker_runs) — detecta corridas colgadas"
      />
      <PanelBody className="p-0">
        <Stateful
          skeleton={<TableSkeleton rows={5} cols={4} />}
          empty={<EmptyState title="Sin corridas de workers" hint="El scheduler todavía no corrió ningún job." />}
        >
          <ul className="divide-y divide-border">
            {items.map(({ job, latest, isStale }) => (
              <li
                key={job}
                className={cn("px-4 py-3", isStale && "bg-status-review/5 ring-1 ring-inset ring-status-review/30")}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    <span className="text-sm font-medium">{JOB_LABEL[job]}</span>
                    {latest ? (
                      <StatusBadge tone={workerTone(latest.status)} label={workerLabel(latest.status)} pulse={latest.status === "running"} />
                    ) : (
                      <span className="eyebrow">sin corridas</span>
                    )}
                    {isStale && (
                      <span className="inline-flex items-center gap-1 rounded border border-status-review/40 bg-status-review/10 px-1.5 py-0.5 text-[10px] font-medium text-status-review">
                        <TriangleAlert className="size-3" /> colgado &gt;30 min
                      </span>
                    )}
                  </div>
                  {latest && (
                    <div className="num flex items-center gap-3 text-xs text-muted-foreground">
                      <span>
                        inició <RelativeTime date={latest.startedAt} />
                      </span>
                      <span>
                        {latest.finishedAt
                          ? formatDurationMs(new Date(latest.finishedAt).getTime() - new Date(latest.startedAt).getTime())
                          : "en curso"}
                      </span>
                    </div>
                  )}
                </div>

                {latest && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {flattenStats(latest.stats).map(({ k, v }) => (
                      <span key={k} className="num rounded bg-muted/50 px-1.5 py-0.5 text-[11px] text-muted-foreground">
                        {k} <span className="font-medium text-foreground">{v}</span>
                      </span>
                    ))}
                  </div>
                )}

                {latest?.status === "error" && latest.error && (
                  <p className="mt-2 font-mono text-[11px] text-status-error">{latest.error}</p>
                )}
              </li>
            ))}
          </ul>
        </Stateful>
      </PanelBody>
    </Panel>
  )
}
