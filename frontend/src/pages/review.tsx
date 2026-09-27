import { PageHeader } from "@/components/common/page-header"
import { ReviewQueue } from "@/components/features/review/review-queue"

export function ReviewPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · revisión"
        title="Cola de revisión"
        description="Una sola bandeja de tareas humanas: mensajes en dead-letter (3 intentos), conflictos de calendario y pares de dedup candidatos. Elegí un ítem para ver el detalle y resolverlo — toda acción es reversible."
      />
      <ReviewQueue />
    </div>
  )
}
