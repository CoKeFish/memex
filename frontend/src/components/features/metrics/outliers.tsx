import { Link } from "react-router-dom"
import { Coins, Timer } from "lucide-react"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState } from "@/components/common/data-state"
import { Skeleton } from "@/components/ui/skeleton"
import { formatDurationMs, formatUsdFine } from "@/lib/format"
import { isBatchModule, moduleLabel } from "@/lib/metrics"
import { fetchLlmCalls, type LlmCallRow, type MetricsWindow } from "@/data"
import { useAsync } from "@/lib/use-async"

function InboxLink({ row }: { row: LlmCallRow }) {
  if (row.inboxId === null) {
    // inbox_id null es batch REAL solo en módulos que agrupan N mensajes (grouped/calendar); en el
    // resto es "sin atribución" — no lo etiquetamos "batch" para no engañar en el debug.
    return isBatchModule(row.module) ? (
      <span className="num text-muted-foreground/60" title="batch: cubre N mensajes">batch</span>
    ) : (
      <span className="num text-muted-foreground/40" title="sin inbox asociado">—</span>
    )
  }
  return (
    <Link to={`/datos/${row.inboxId}`} className="num text-origin-inbox hover:underline">
      #{row.inboxId}
    </Link>
  )
}

function TopCalls({
  window: win,
  sort,
  title,
  eyebrow,
  icon,
  metric,
}: {
  window: MetricsWindow
  sort: "cost_usd" | "latency_ms"
  title: string
  eyebrow: string
  icon: React.ReactNode
  metric: (r: LlmCallRow) => string
}) {
  const { data, loading } = useAsync(
    () => fetchLlmCalls({ ...win, sort, dir: "desc", limit: 6 }),
    [win.since, win.until, win.tz, sort],
  )
  const rows = data?.items ?? []
  return (
    <Panel>
      <PanelHeader eyebrow={eyebrow} title={title} right={icon} />
      <PanelBody className="p-0">
        {loading && !data ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-5 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="Sin llamadas en el rango" />
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((r) => (
              <li key={r.id} className="flex items-center justify-between gap-3 px-4 py-2 text-xs">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="shrink-0 font-medium">{moduleLabel(r.module)}</span>
                  <span className="num truncate text-muted-foreground">{r.model}</span>
                </span>
                <span className="flex shrink-0 items-center gap-3">
                  <InboxLink row={r} />
                  <span className="num font-medium tabular-nums">{metric(r)}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

/** Outliers del rango: las llamadas más caras y las más lentas (cazar gasto y latencia atípicos). */
export function Outliers({ window: win }: { window: MetricsWindow }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <TopCalls
        window={win}
        sort="cost_usd"
        eyebrow="Outliers · costo"
        title="Llamadas más caras"
        icon={<Coins className="size-4 text-muted-foreground" />}
        metric={(r) => formatUsdFine(r.costUsd)}
      />
      <TopCalls
        window={win}
        sort="latency_ms"
        eyebrow="Outliers · latencia"
        title="Llamadas más lentas"
        icon={<Timer className="size-4 text-muted-foreground" />}
        metric={(r) => formatDurationMs(r.latencyMs)}
      />
    </div>
  )
}
