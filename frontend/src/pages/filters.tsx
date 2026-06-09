import { PageHeader } from "@/components/common/page-header"
import { FiltersManager } from "@/components/features/control/filters-manager"

export function FiltersPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · filtros"
        title="Filtros"
        description="Qué entra y qué se descarta: reglas de filtro pre-ingest que cortan los próximos registros (no los ya recibidos)."
      />
      <FiltersManager />
    </div>
  )
}
