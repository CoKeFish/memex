import { ArrowRight, ListChecks, Inbox, OctagonAlert, TriangleAlert } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { CostKpis } from "@/components/features/metrics/cost-kpis"
import { FreshnessGrid } from "@/components/features/pipeline/freshness-grid"
import { formatInt } from "@/lib/format"
import { inboxErrorCount, inboxPendingCount, reviewCount, staleWorkerCount } from "@/data"
import { useAlerts } from "@/state/alerts"
import type { Tone } from "@/lib/status"
import type { AlertSeverity } from "@/types/domain"

const sevTone: Record<AlertSeverity, Tone> = { critica: "error", alta: "review", info: "running" }

export function OverviewPage() {
  const { alerts, unread } = useAlerts()
  const critical = alerts.find((a) => a.severity === "critica" && !a.read)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · resumen"
        title="Resumen"
        description="El estado del sistema de un vistazo: lo que pide tu atención, el gasto del LLM y qué tan fresco está cada flujo."
      />

      {critical && (
        <Link
          to={critical.deepLink}
          className="flex items-center gap-3 rounded-lg border border-status-error/40 bg-status-error/10 px-4 py-3 text-sm transition-colors hover:bg-status-error/15"
        >
          <OctagonAlert className="size-5 shrink-0 text-status-error" />
          <div className="min-w-0 flex-1">
            <span className="font-medium text-status-error">{critical.title}</span>
            <span className="ml-2 text-muted-foreground">{critical.detail}</span>
          </div>
          <ArrowRight className="size-4 shrink-0 text-muted-foreground" />
        </Link>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <OpsCard to="/revision" eyebrow="Pendiente de revisión" value={reviewCount()} icon={ListChecks} tone="review" hint="dead-letter + conflictos + dedup" />
        <OpsCard to="/datos" eyebrow="Inbox pendiente" value={inboxPendingCount()} icon={Inbox} tone="running" hint="mensajes sin procesar" />
        <OpsCard to="/datos" eyebrow="Inbox con error" value={inboxErrorCount()} icon={TriangleAlert} tone="error" hint="process_error ≠ null" />
        <OpsCard to="/pipeline" eyebrow="Workers colgados" value={staleWorkerCount()} icon={OctagonAlert} tone={staleWorkerCount() > 0 ? "review" : "ok"} hint="running >30 min" />
      </div>

      <div>
        <div className="eyebrow mb-2">costo del LLM · rango actual</div>
        <CostKpis />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1fr_1.1fr]">
        <Panel className="overflow-hidden">
          <PanelHeader eyebrow={`alertas · ${unread} sin leer`} title="Lo último que requiere atención" />
          <PanelBody className="p-0">
            <ul className="divide-y divide-border">
              {alerts.slice(0, 5).map((a) => (
                <li key={a.id}>
                  <Link to={a.deepLink} className="flex items-start gap-3 px-4 py-3 hover:bg-accent/30">
                    <Led tone={sevTone[a.severity]} className="mt-1.5" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className={cn("truncate text-sm", a.read ? "text-muted-foreground" : "font-medium")}>{a.title}</span>
                        <span className="shrink-0 text-[11px] text-muted-foreground"><RelativeTime date={a.at} /></span>
                      </div>
                      <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">{a.detail}</p>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          </PanelBody>
        </Panel>
        <FreshnessGrid />
      </div>
    </div>
  )
}

function OpsCard({
  to,
  eyebrow,
  value,
  icon: Icon,
  tone,
  hint,
}: {
  to: string
  eyebrow: string
  value: number
  icon: typeof Inbox
  tone: Tone
  hint: string
}) {
  const toneText: Record<Tone, string> = {
    ok: "text-status-ok",
    error: "text-status-error",
    running: "text-status-running",
    filtered: "text-status-filtered",
    review: "text-status-review",
    pending: "text-status-pending",
    neutral: "text-muted-foreground",
  }
  return (
    <Link to={to} className="group">
      <Panel className="p-4 transition-colors group-hover:border-brand/40">
        <div className="flex items-center justify-between">
          <span className="eyebrow">{eyebrow}</span>
          <Icon className={cn("size-4", toneText[tone])} />
        </div>
        <div className="mt-2 flex items-end justify-between">
          <span className={cn("kpi text-3xl leading-none", value > 0 ? toneText[tone] : "text-foreground")}>{formatInt(value)}</span>
          <ArrowRight className="size-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
        </div>
        <div className="mt-2 text-xs text-muted-foreground">{hint}</div>
      </Panel>
    </Link>
  )
}
