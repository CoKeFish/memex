import { PageHeader } from "@/components/common/page-header"
import { FiltersManager } from "@/components/features/control/filters-manager"
import { SocialMonitor } from "@/components/features/control/social-monitor"

export function FiltersPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · filtros"
        title="Filtros"
        description="Qué entra y qué se descarta. Reglas de filtro pre-ingest (cortan los próximos, no los ya recibidos) y las redes sociales monitoreadas vía API (a quién seguir por red)."
      />
      <SocialMonitor />
      <FiltersManager />
    </div>
  )
}
