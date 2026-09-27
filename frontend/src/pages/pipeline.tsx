import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { ErrorState } from "@/components/common/data-state"
import { FreshnessGrid } from "@/components/features/pipeline/freshness-grid"
import { IngestionRuns } from "@/components/features/pipeline/ingestion-runs"
import { SourcesHealth } from "@/components/features/pipeline/sources-health"
import { WorkersBoard } from "@/components/features/pipeline/workers-board"
import { fetchPipeline, rangeKeyWindow } from "@/data"
import { useAsync } from "@/lib/use-async"
import { useTimeRange } from "@/state/time-range"

export function PipelinePage() {
  const { range } = useTimeRange()
  const { data, loading, error, reload } = useAsync(() => fetchPipeline(rangeKeyWindow(range)), [range])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · pipeline"
        title="Observabilidad del pipeline"
        description="El flujo end-to-end de un vistazo: qué tan fresco está cada fuente y worker, salud de la ingesta, estado de los workers (incluidas corridas colgadas) y el invariante de contabilidad de las corridas."
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando pipeline…
        </div>
      ) : !data ? null : (
        <>
          <FreshnessGrid sources={data.sources} workers={data.workers} />
          <div className="grid gap-5 xl:grid-cols-2">
            <SourcesHealth items={data.sources} />
            <WorkersBoard items={data.workers} />
          </div>
          <IngestionRuns runs={data.ingestion.runs} totals={data.ingestion.totals} />
        </>
      )}
    </div>
  )
}
