import { PageHeader } from "@/components/common/page-header"
import {
  ManualRunPanel,
  ModulesTogglePanel,
  SchedulerPanel,
  SourcesTogglePanel,
} from "@/components/features/control/processing-controls"

export function ProcessingPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · procesamiento"
        title="Procesamiento"
        description="Prender/apagar el procesamiento automático y correr cada etapa a mano. Hoy: scheduler por config (off por defecto, nada corre solo) + CLIs idempotentes; los badges indican qué ya existe vs qué falta cablear por HTTP. Las reglas de filtro se movieron a la sección Filtros."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <SchedulerPanel />
        <ManualRunPanel />
      </div>
      <div className="grid gap-5 xl:grid-cols-2">
        <SourcesTogglePanel />
        <ModulesTogglePanel />
      </div>
    </div>
  )
}
