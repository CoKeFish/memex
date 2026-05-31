import { cn } from "@/lib/utils"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatInt } from "@/lib/format"
import { ingestionLabel, ingestionTone } from "@/lib/status"
import { ingestionTotals, ingestionWithInvariant, SOURCE_BY_ID } from "@/data"
import { useTimeRange } from "@/state/time-range"

export function IngestionRuns() {
  const { range } = useTimeRange()
  const runs = ingestionWithInvariant(range).slice(0, 16)
  const totals = ingestionTotals(range)

  return (
    <Panel>
      <PanelHeader
        eyebrow="Pipeline · corridas"
        title="Corridas de ingesta e invariante"
        sub="posted = inserted + duplicates + errors + filtered (migración 0004)"
        right={
          totals.unbalanced > 0 ? (
            <StatusBadge tone="error" label={`${totals.unbalanced} descuadre`} />
          ) : (
            <StatusBadge tone="ok" label="invariante OK" />
          )
        }
      />
      <PanelBody className="p-0">
        <Stateful
          skeleton={<TableSkeleton rows={8} cols={6} />}
          empty={<EmptyState title="Sin corridas en el rango" />}
        >
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/30 text-left">
                  <th className="px-3 py-2 font-medium text-muted-foreground">Source</th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Estado</th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Inició</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Posted</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Ins.</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Dup.</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Err.</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Filt.</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Invariante</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {runs.map((r) => (
                  <tr key={r.id} className={cn("hover:bg-accent/30", !r.balanced && "bg-status-error/5")}>
                    <td className="whitespace-nowrap px-3 py-2 font-medium">{SOURCE_BY_ID[r.sourceId]?.name ?? r.sourceId}</td>
                    <td className="px-3 py-2">
                      <StatusBadge tone={ingestionTone(r.status)} label={ingestionLabel(r.status)} pulse={r.status === "running"} />
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                      <RelativeTime date={r.startedAt} />
                    </td>
                    <td className="num px-3 py-2 text-right font-medium">{formatInt(r.posted)}</td>
                    <td className="num px-3 py-2 text-right text-status-ok">{formatInt(r.inserted)}</td>
                    <td className="num px-3 py-2 text-right text-muted-foreground">{formatInt(r.duplicates)}</td>
                    <td className={cn("num px-3 py-2 text-right", r.errors > 0 ? "text-status-error" : "text-muted-foreground")}>
                      {formatInt(r.errors)}
                    </td>
                    <td className="num px-3 py-2 text-right text-status-filtered">{formatInt(r.filtered)}</td>
                    <td className="px-3 py-2 text-right">
                      {r.balanced ? (
                        <span className="num text-status-ok">OK</span>
                      ) : (
                        <span
                          className="num font-medium text-status-error"
                          title={`posted ${r.posted} ≠ suma ${r.expected}`}
                        >
                          Δ {r.posted - r.expected > 0 ? "+" : ""}
                          {r.posted - r.expected}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Stateful>
      </PanelBody>
    </Panel>
  )
}
