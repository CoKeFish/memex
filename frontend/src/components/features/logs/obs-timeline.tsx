import { Panel } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchObsTimeline } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { ObsKind } from "@/types/domain"

const KIND_LABEL: Record<ObsKind, string> = {
  ingestion: "Ingesta",
  worker: "Worker",
  llm: "LLM",
  failure: "Falla",
  calendar: "Calendario",
}

/** Timeline derivado de la observabilidad PERSISTIDA del pipeline (ingestion_runs + worker_runs vía
 * /stats/pipeline) — no de los logs efímeros. Ordenado por recencia; refresca con la topbar. */
export function ObsTimeline() {
  const { data, loading, error, reload } = useAsync(() => fetchObsTimeline(), [])
  const entries = data ?? []

  if (error) {
    return (
      <Panel>
        <ErrorState detail={error} onRetry={reload} />
      </Panel>
    )
  }
  if (loading && !data) {
    return (
      <Panel>
        <div className="space-y-2 p-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      </Panel>
    )
  }
  if (entries.length === 0) {
    return (
      <Panel>
        <EmptyState title="Sin actividad" hint="Todavía no hay corridas registradas." />
      </Panel>
    )
  }

  return (
    <Panel className="overflow-hidden">
      <ul className="max-h-[600px] divide-y divide-border overflow-y-auto">
        {entries.map((e) => (
          <li key={e.id} className="flex items-start gap-3 px-4 py-2.5">
            <Led tone={e.tone} className="mt-1.5" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-sm">
                <span className="eyebrow shrink-0">{KIND_LABEL[e.kind]}</span>
                <span className="truncate font-medium">{e.title}</span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  <RelativeTime date={e.ts} />
                </span>
              </div>
              <p className="num mt-0.5 truncate text-[11px] text-muted-foreground">{e.detail}</p>
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  )
}
