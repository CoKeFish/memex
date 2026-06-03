import { PageHeader } from "@/components/common/page-header"
import {
  DetectadasPanel,
  OrgsPanel,
  PersonsPanel,
  SyncPanel,
} from "@/components/features/identidades/identidades-panels"

export function IdentidadesPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · identidades"
        title="Directorio"
        description="Una lista de identidades: personas (de Google Contacts) y organizaciones / productos / agentes (Unity, Claude, …). Cada una en interés o Detectada: lo que el sistema encuentra en tus correos, chats y social entra como «Detectada» y vos la promovés a interés con un clic."
      />
      <DetectadasPanel />
      <div className="grid gap-5 xl:grid-cols-2">
        <PersonsPanel />
        <OrgsPanel />
      </div>
      <SyncPanel />
    </div>
  )
}
