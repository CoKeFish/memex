import { PageHeader } from "@/components/common/page-header"
import { InboxFeed } from "@/components/features/data/inbox-feed"

export function DataPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · datos"
        title="Exploración de datos"
        description="El inbox crudo, navegable sin SQL: filtrá por fuente y estado, buscá texto, y leé el contenido con el mismo render agnóstico (render_payload) que ven el summarizer y los módulos. La lista está virtualizada sobre 2.000 mensajes."
      />
      <InboxFeed />
    </div>
  )
}
