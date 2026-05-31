import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import { AdHocIngest, FetchControl } from "@/components/features/control/fetch-control"

export function IngestPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · carga"
        title="Carga / ingesta"
        description="Traer correos a demanda (incremental por checkpoint hoy; por rango/cantidad cuando el ingestor lo soporte) e ingesta puntual. El dry-run muestra la idempotencia: los correos ya guardados se ignoran, no se duplican."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <FetchControl />
        <AdHocIngest />
      </div>
      <Panel>
        <PanelHeader eyebrow="idempotencia" title="¿Cómo sabe si ya tiene el correo?" />
        <PanelBody>
          <ul className="space-y-2 text-sm">
            {[
              ["existe", "Al insertar: UNIQUE(source_id, external_id) + ON CONFLICT DO NOTHING → si el external_id (UID) ya está, no se inserta; cuenta como duplicate en ingestion_runs."],
              ["existe", "Checkpoint por fuente (uidvalidity / last_uid) → el fetch incremental ni siquiera vuelve a bajar lo viejo."],
              ["existe", "Downstream idempotente: classify/summarize/extract cursorean por AUSENCIA de fila (LEFT JOIN) + UNIQUE(inbox_id) / UNIQUE(module_slug, inbox_id) → no re-procesan lo ya hecho."],
              ["gap", "No hay dedup por Message-ID: el MISMO correo llegando por dos cuentas/UIDs distintos entraría dos veces (distinto external_id)."],
            ].map(([level, text], i) => (
              <li key={i} className="flex items-start gap-2.5">
                <Led tone={level === "gap" ? "review" : "ok"} className="mt-1.5" />
                <span className={level === "gap" ? "text-muted-foreground" : ""}>{text}</span>
              </li>
            ))}
          </ul>
        </PanelBody>
      </Panel>
    </div>
  )
}
