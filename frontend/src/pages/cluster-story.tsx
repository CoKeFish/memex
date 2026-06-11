// Story de un cúmulo: su cronología en pantalla completa. Título = nombre del cúmulo, sinopsis = su
// descripción (la del LLM), y los sucesos fechados en un eje vertical (componente `ClusterTimeline`).

import { ArrowLeft, Clock } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { PageHeader } from "@/components/common/page-header"
import { Skeleton } from "@/components/ui/skeleton"
import { ClusterTimeline } from "@/components/features/graph/cluster-timeline"
import { fetchClusterTimeline } from "@/data/graph"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

const BackToGraph = (
  <Link
    to="/grafo"
    className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium hover:bg-muted/60"
  >
    <ArrowLeft className="size-3.5" /> Grafo
  </Link>
)

export function ClusterStoryPage() {
  const { id } = useParams<{ id: string }>()
  const clusterId = Number(id)
  const { data, error, loading, reload } = useAsync(
    () => fetchClusterTimeline(clusterId),
    [clusterId],
  )

  if (error) {
    return (
      <div>
        <PageHeader eyebrow="Cronología" title="Cúmulo" actions={BackToGraph} />
        <ErrorState detail={errMsg(error)} onRetry={reload} />
      </div>
    )
  }
  if (loading && !data) {
    return (
      <div>
        <PageHeader eyebrow="Cronología" title="Cargando…" actions={BackToGraph} />
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      </div>
    )
  }
  if (!data) return null

  const { cluster, events, actors } = data
  return (
    <div className="max-w-3xl">
      <PageHeader
        eyebrow="Cronología"
        title={cluster.name || "Cúmulo"}
        description={cluster.description || undefined}
        actions={BackToGraph}
      />
      <div className="mb-5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <span className="num">{cluster.memberCount} miembros</span>
        <span>·</span>
        <span className="num">{events.length} sucesos</span>
        {cluster.confidence != null && (
          <>
            <span>·</span>
            <span className="num">confianza {cluster.confidence.toFixed(2)}</span>
          </>
        )}
      </div>
      {events.length === 0 && actors.length === 0 ? (
        <EmptyState
          icon={<Clock className="size-5" />}
          title="Sin sucesos"
          hint="Este cúmulo no tiene miembros con fecha ni elenco."
        />
      ) : (
        <ClusterTimeline events={events} actors={actors} inboxKinds={data.inboxKinds} />
      )}
    </div>
  )
}
