import { PageHeader } from "@/components/common/page-header"
import { FiltersDocs } from "@/components/features/control/filters-docs"
import { FiltersManager } from "@/components/features/control/filters-manager"
import { RelevanceGateManager } from "@/components/features/control/relevance-gate-manager"
import { RelevanceRulesManager } from "@/components/features/control/relevance-rules-manager"
import { SenderTiersManager } from "@/components/features/control/sender-tiers-manager"

export function FiltersPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · filtros"
        title="Filtros"
        description="Qué entra y cómo se procesa: reglas pre-ingest que descartan antes de guardar, tiers por remitente que regulan el gasto LLM y el gate de relevancia por intereses (correos). Todo prospectivo: afecta lo próximo, no lo ya recibido."
      />
      <FiltersManager />
      <SenderTiersManager />
      <RelevanceGateManager />
      <RelevanceRulesManager />
      <FiltersDocs />
    </div>
  )
}
