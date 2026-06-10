import { PageHeader } from "@/components/common/page-header"
import { CollapsiblePanel } from "@/components/common/collapsible-panel"
import { Led } from "@/components/common/led"
import { AdHocIngest, FetchControl } from "@/components/features/control/fetch-control"
import { BackfillPanel } from "@/components/features/control/backfill-control"
import {
  IngestRunsPanel,
  IngestSchedulerPanel,
} from "@/components/features/control/ingest-schedule-control"

// Orden de la vista: lo operativo del día a día arriba (traer a demanda, con las cuentas seguidas
// adentro de cada red), las herramientas puntuales colapsadas (histórico, ad-hoc, explicación).
export function IngestPage() {
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="vista · carga" title="Carga / ingesta" />
      <FetchControl />
      <IngestSchedulerPanel />
      <IngestRunsPanel />
      <BackfillPanel />
      <AdHocIngest />
      <CollapsiblePanel
        eyebrow="duplicados"
        title="¿Cómo evita guardar el mismo correo dos veces?"
      >
        <ul className="space-y-2 text-sm">
          {[
            ["existe", "Si un correo ya fue guardado, se ignora y no se vuelve a insertar (cuenta como duplicado)."],
            ["existe", "Cada fuente recuerda hasta dónde trajo, así el modo incremental ni siquiera vuelve a descargar lo viejo."],
            ["existe", "Las etapas siguientes (clasificar, resumir, extraer) tampoco repiten trabajo: saltan lo que ya procesaron."],
            ["gap", "Límite conocido: el mismo correo que llega por dos cuentas distintas entra dos veces (no se detecta que es el mismo)."],
          ].map(([level, text], i) => (
            <li key={i} className="flex items-start gap-2.5">
              <Led tone={level === "gap" ? "review" : "ok"} className="mt-1.5" />
              <span className={level === "gap" ? "text-muted-foreground" : ""}>{text}</span>
            </li>
          ))}
        </ul>
      </CollapsiblePanel>
    </div>
  )
}
