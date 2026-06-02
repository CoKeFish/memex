import { Link } from "react-router-dom"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState } from "@/components/common/data-state"
import { Skeleton } from "@/components/ui/skeleton"
import { RelativeTime } from "@/components/common/time"
import { moduleLabel } from "@/lib/metrics"
import { fetchLlmCalls, type MetricsWindow } from "@/data"
import { useAsync } from "@/lib/use-async"

/** Panel dedicado de llamadas con error en el rango (status=error + su mensaje), para debug rápido.
 *  `module` opcional: acota los errores a un módulo (p. ej. "finance" para la vista de finanzas). */
export function RecentErrors({ window: win, module }: { window: MetricsWindow; module?: string }) {
  const { data, loading } = useAsync(
    () =>
      fetchLlmCalls({
        ...win,
        status: ["error"],
        module: module ? [module] : undefined,
        sort: "created_at",
        dir: "desc",
        limit: 10,
      }),
    [win.since, win.until, module],
  )
  const rows = data?.items ?? []
  return (
    <Panel>
      <PanelHeader
        eyebrow="Debug · errores"
        title="Errores recientes"
        sub="Llamadas con status=error y su mensaje"
        right={data ? <span className="num text-xs text-muted-foreground">{data.total} en el rango</span> : undefined}
      />
      <PanelBody className="p-0">
        {loading && !data ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-6 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="Sin errores en el rango" hint="Ninguna llamada falló en esta ventana." />
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((r) => (
              <li key={r.id} className="flex items-start justify-between gap-3 px-4 py-2.5 text-xs">
                <span className="min-w-0">
                  <span className="flex items-center gap-2">
                    <span className="font-medium">{moduleLabel(r.module)}</span>
                    <span className="num text-muted-foreground">{r.model}</span>
                    {r.inboxId !== null && (
                      <Link to={`/datos/${r.inboxId}`} className="num text-origin-inbox hover:underline">
                        #{r.inboxId}
                      </Link>
                    )}
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[11px] text-status-error" title={r.errorMessage ?? undefined}>
                    {r.errorMessage ?? "—"}
                  </span>
                </span>
                <RelativeTime date={r.createdAt} className="shrink-0 text-muted-foreground" />
              </li>
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}
