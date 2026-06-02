import { PageHeader } from "@/components/common/page-header"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { EventStream } from "@/components/features/logs/event-stream"
import { ObsTimeline } from "@/components/features/logs/obs-timeline"
import { MetricsTzProvider } from "@/state/metrics-tz"

export function LogsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · logs"
        title="Logs"
        description="Eventos del sistema para debug, con datos reales: ahora cada evento structlog se persiste a la tabla log_events (sink real, migración 0020) y se consulta vía /logs — con métricas del rango, filtros por nivel/logger/evento, búsqueda y tail en vivo. El tab Observabilidad muestra el timeline de lo persistido por el pipeline (ingestas y workers). Activá el auto-refresco (arriba) o el tail en vivo para verlo en tiempo real."
      />
      <Tabs defaultValue="stream">
        <TabsList>
          <TabsTrigger value="stream">Stream de eventos</TabsTrigger>
          <TabsTrigger value="obs">Observabilidad</TabsTrigger>
        </TabsList>
        {/* El stream usa MetricsFilters (rango + TZ), que lee la TZ activa del provider. EventStream
            renderiza LogMetrics arriba del stream, ambos alimentados por los mismos filtros. */}
        <TabsContent value="stream" className="mt-4">
          <MetricsTzProvider>
            <EventStream />
          </MetricsTzProvider>
        </TabsContent>
        <TabsContent value="obs" className="mt-4">
          <ObsTimeline />
        </TabsContent>
      </Tabs>
    </div>
  )
}
