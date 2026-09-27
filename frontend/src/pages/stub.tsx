import { Construction } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { Panel } from "@/components/common/panel"

export function StubView({
  eyebrow,
  title,
  description,
  features,
}: {
  eyebrow: string
  title: string
  description: string
  features: string[]
}) {
  return (
    <div>
      <PageHeader eyebrow={eyebrow} title={title} description={description} />
      <Panel className="p-6">
        <div className="flex items-center gap-2">
          <Construction className="size-4 text-brand" />
          <span className="eyebrow">Stub · maqueta pendiente</span>
        </div>
        <p className="mt-3 text-sm text-muted-foreground">
          Esta categoría quedó como ruta stub en esta sesión (shell + primer slice P0). Características del
          catálogo previstas para esta sección:
        </p>
        <ul className="mt-4 grid gap-2 sm:grid-cols-2">
          {features.map((f) => (
            <li
              key={f}
              className="flex items-start gap-2.5 rounded-md border border-border bg-muted/30 px-3 py-2 text-sm"
            >
              <span className="led mt-1.5 text-status-pending" style={{ width: 6, height: 6 }} />
              <span>{f}</span>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  )
}
