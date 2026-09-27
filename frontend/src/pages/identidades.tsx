import { useState } from "react"
import { PageHeader } from "@/components/common/page-header"
import {
  DirectoryPanel,
  HierarchyPanel,
  IdentityDetailPanel,
  MergeReviewPanel,
  SyncPanel,
} from "@/components/features/identidades/identidades-panels"

export function IdentidadesPage() {
  const [selectedId, setSelectedId] = useState<number | null>(null)
  // Un contador compartido: cualquier mutación lo incrementa y todos los paneles recargan
  // (cada uno lo incluye en las deps de su `useAsync`).
  const [version, setVersion] = useState(0)
  const bump = (): void => setVersion((v) => v + 1)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · identidades"
        title="Directorio"
        description="Identidades unificadas: personas, organizaciones y productos, cada una con sus identificadores por-fuente (email, handles por red, dominios) y, para empresas, sus sedes. Lo que el sistema detecta en tus correos/chats/social entra como «Detectada» y la promovés a interés con un clic. Los posibles duplicados que el dedup difuso no fusiona solo quedan para tu revisión."
      />
      <MergeReviewPanel refresh={version} onChanged={bump} />
      <HierarchyPanel refresh={version} onSelect={setSelectedId} />
      <div className="grid gap-5 xl:grid-cols-2">
        <DirectoryPanel
          selectedId={selectedId}
          onSelect={setSelectedId}
          refresh={version}
          onChanged={bump}
        />
        <IdentityDetailPanel
          id={selectedId}
          refresh={version}
          onChanged={bump}
          onDeleted={() => setSelectedId(null)}
          onSelect={setSelectedId}
        />
      </div>
      <SyncPanel />
    </div>
  )
}
