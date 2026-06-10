import { PageHeader } from "@/components/common/page-header"
import { FiltersDocs } from "@/components/features/control/filters-docs"
import { FiltersManager } from "@/components/features/control/filters-manager"
import { SenderTiersManager } from "@/components/features/control/sender-tiers-manager"

export function FiltersPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · filtros"
        title="Filtros"
        description="Qué entra y cómo se procesa: reglas pre-ingest que descartan antes de guardar y tiers por remitente que regulan el gasto LLM. Todo prospectivo: afecta lo próximo, no lo ya recibido."
      />
      <FiltersManager />
      <SenderTiersManager />
      <FiltersDocs />
    </div>
  )
}
