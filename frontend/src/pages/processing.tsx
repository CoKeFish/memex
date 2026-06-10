import { PageHeader } from "@/components/common/page-header"
import { ManualRunPanel } from "@/components/features/control/manual-run"
import {
  ModulesTogglePanel,
  SchedulerPanel,
} from "@/components/features/control/processing-controls"

export function ProcessingPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · procesamiento"
        title="Procesamiento"
        description="Decidí qué procesar: elegí etapas y acotá por fuente, fecha o cantidad, con dry-run previo; las corridas van en background. Prendé/apagá el procesamiento automático (off por defecto, nada corre solo) y los módulos de extracción. El control de la ingesta por fuente (habilitar + cada cuánto) vive en Carga. Las reglas de filtro persistentes viven en Filtros."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <SchedulerPanel />
        <ManualRunPanel />
      </div>
      <ModulesTogglePanel />
    </div>
  )
}
