import { PageHeader } from "@/components/common/page-header"
import {
  ApiAccessPanel,
  CliAccessPanel,
  IdentityPanel,
  ProvidersPanel,
  RoadmapPanel,
} from "@/components/features/account/panels"
import { getAccount } from "@/data"

export function AccountPage() {
  const a = getAccount()
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · cuenta"
        title="Cuenta y acceso"
        description="Quién sos en el sistema, cómo se autentican el API y el CLI, y qué cuentas externas están conectadas. Todos los secretos van enmascarados (la DB nunca guarda el token, solo el nombre de su env var)."
      />
      <div className="grid gap-5 lg:grid-cols-2">
        <IdentityPanel identity={a.identity} />
        <CliAccessPanel cli={a.cli} />
        <ApiAccessPanel api={a.api} />
        <ProvidersPanel providers={a.providers} imap={a.imap} />
        <RoadmapPanel />
      </div>
    </div>
  )
}
