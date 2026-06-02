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
        description="Eventos del sistema para debug, con datos reales: el stream reconstruido de las llamadas LLM (tabla llm_calls, filtrable por módulo) y un timeline de la observabilidad persistida del pipeline (ingestas y workers). Activá el auto-refresco (arriba) para verlo en vivo."
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
