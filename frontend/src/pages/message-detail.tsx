import { useState } from "react"
import { ArrowLeft } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { EmptyState } from "@/components/common/data-state"
import { StatusBadge } from "@/components/common/led"
import { Panel, PanelHeader } from "@/components/common/panel"
import { RelativeTime } from "@/components/common/time"
import { JourneyTimeline } from "@/components/features/message/journey-timeline"
import { RelatedData } from "@/components/features/message/related-data"
import { ReprocessButton, type ReprocessStep } from "@/components/features/message/reprocess-button"
import { LogRow } from "@/components/features/logs/log-row"
import { getMessageJourney, SOURCE_BY_ID } from "@/data"
import { renderPayload } from "@/lib/render-payload"
import type { Tone } from "@/lib/status"
import type { InboxRow } from "@/types/domain"

function statusOf(row: InboxRow): { tone: Tone; label: string } {
  if (row.processError) return { tone: "error", label: "Error" }
  if (row.processedAt) return { tone: "ok", label: "Procesado" }
  return { tone: "pending", label: "Pendiente" }
}

function BackLink() {
  return (
    <Button variant="ghost" size="sm" className="h-8" asChild>
      <Link to="/datos">
        <ArrowLeft className="size-4" /> Datos
      </Link>
    </Button>
  )
}

export function MessageDetailPage() {
  const { id } = useParams()
  const journey = getMessageJourney(Number(id))
  const [raw, setRaw] = useState(false)

  if (!journey) {
    return (
      <div className="space-y-4">
        <BackLink />
        <Panel>
          <EmptyState title="Mensaje no encontrado" hint={`No existe el inbox #${id}.`} />
        </Panel>
      </div>
    )
  }

  const { row, steps, logs, related } = journey
  const src = SOURCE_BY_ID[row.sourceId]
  const rendered = renderPayload(row.payload, row.ocrText ?? "")
  const st = statusOf(row)

  const reprocessSteps: ReprocessStep[] = [
    { key: "clasificar", label: "Re-clasificar", cursor: "classifications", cost: "US$0 (reglas)" },
    ...(steps.some((s) => s.kind === "resumen")
      ? [{ key: "resumir", label: "Re-resumir", cursor: "summary_inbox_links", cost: "~US$0.002" }]
      : []),
    ...(steps.some((s) => s.kind === "modulo")
      ? [{ key: "extraer", label: "Re-extraer (módulos)", cursor: "module_extractions", cost: "~US$0.004" }]
      : []),
    ...(journey.media.length > 0
      ? [{ key: "ocr", label: "Re-OCR de adjuntos", cursor: "media_assets.ocr_status", cost: "~US$0.015/img" }]
      : []),
  ]

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <BackLink />
        <span className="eyebrow">camino de decisión</span>
      </div>

      <Panel className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="num text-sm text-muted-foreground">inbox #{row.id}</span>
              <span className="text-sm font-semibold text-origin-inbox">{src?.name ?? row.sourceId}</span>
              <StatusBadge tone={st.tone} label={st.label} />
            </div>
            <div className="num mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
              <span>{row.externalId}</span>
              <span>occurred <RelativeTime date={row.occurredAt} /></span>
              <span>received <RelativeTime date={row.receivedAt} /></span>
              {row.attempts > 0 && <span className="text-status-error">{row.attempts} intentos</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <ReprocessButton inboxId={row.id} steps={reprocessSteps} />
            <Label htmlFor="raw-detail" className="eyebrow cursor-pointer">JSON crudo</Label>
            <Switch id="raw-detail" checked={raw} onCheckedChange={setRaw} />
          </div>
        </div>
        {raw ? (
          <pre className="mt-3 max-h-64 overflow-auto rounded-md border border-border bg-muted/30 p-3 font-mono text-[11px] text-muted-foreground">
            {JSON.stringify(row.payload, null, 2)}
          </pre>
        ) : (
          <div className="mt-3 rounded-md border border-border bg-muted/20 p-3 text-sm">
            {rendered.sender && <div className="mb-1 font-medium">{rendered.sender}</div>}
            <p className="whitespace-pre-wrap text-muted-foreground">{rendered.body || "(sin texto)"}</p>
          </div>
        )}
      </Panel>

      <div className="grid gap-5 xl:grid-cols-[1.5fr_1fr]">
        <div>
          <div className="eyebrow mb-3">camino de decisión · {steps.length} etapas</div>
          <JourneyTimeline steps={steps} />
        </div>
        <div className="space-y-5">
          <RelatedData related={related} />
          <Panel className="overflow-hidden">
            <PanelHeader
              eyebrow="logs correlacionados"
              title="Eventos de esta request"
              sub={`request_id compartido · ${logs.length} eventos structlog`}
            />
            <div className="max-h-[380px] overflow-y-auto">
              {logs.map((l) => (
                <LogRow key={l.id} event={l} />
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}
