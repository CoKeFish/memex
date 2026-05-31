import { PageHeader } from "@/components/common/page-header"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { EventStream } from "@/components/features/logs/event-stream"
import { ObsTimeline } from "@/components/features/logs/obs-timeline"

export function LogsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · logs"
        title="Logs"
        description="Eventos del sistema para debug: el stream de logs structlog (efímeros, vía API/CLI en real) y un timeline derivado de la observabilidad persistida. Activá el auto-refresco (arriba) para ver el stream en vivo."
      />
      <Tabs defaultValue="stream">
        <TabsList>
          <TabsTrigger value="stream">Stream de eventos</TabsTrigger>
          <TabsTrigger value="obs">Observabilidad</TabsTrigger>
        </TabsList>
        <TabsContent value="stream" className="mt-4">
          <EventStream />
        </TabsContent>
        <TabsContent value="obs" className="mt-4">
          <ObsTimeline />
        </TabsContent>
      </Tabs>
    </div>
  )
}
