import { PageHeader } from "@/components/common/page-header"
import { FreshnessGrid } from "@/components/features/pipeline/freshness-grid"
import { IngestionRuns } from "@/components/features/pipeline/ingestion-runs"
import { SourcesHealth } from "@/components/features/pipeline/sources-health"
import { WorkersBoard } from "@/components/features/pipeline/workers-board"

export function PipelinePage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · pipeline"
        title="Observabilidad del pipeline"
        description="El flujo end-to-end de un vistazo: qué tan fresco está cada fuente y worker, salud de la ingesta, estado de los workers (incluidas corridas colgadas) y el invariante de contabilidad de las corridas."
      />
      <FreshnessGrid />
      <div className="grid gap-5 xl:grid-cols-2">
        <SourcesHealth />
        <WorkersBoard />
      </div>
      <IngestionRuns />
    </div>
  )
}
