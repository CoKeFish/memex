import { TriangleAlert } from "lucide-react"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatCompact, formatInt, formatUsd, formatUsdFine, formatPct } from "@/lib/format"
import { costByModel, costByPurpose } from "@/lib/selectors"
import { useTimeRange } from "@/state/time-range"

function PurposeBars() {
  const { range } = useTimeRange()
  const rows = costByPurpose(range)
  const max = Math.max(...rows.map((r) => r.cost), 0.0001)
  const total = rows.reduce((a, r) => a + r.cost, 0)
  return (
    <div className="space-y-3">
      {rows.map((r) => (
        <div key={r.purpose}>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="flex items-center gap-2">
              <span className="size-2 rounded-[2px]" style={{ background: r.chart }} />
              <span className="font-medium">{r.label}</span>
              <span className="num text-muted-foreground">{formatInt(r.calls)} ll.</span>
            </span>
            <span className="num font-medium">
              {formatUsd(r.cost)}
              <span className="ml-1.5 text-muted-foreground">{total ? formatPct(r.cost / total, 0) : "0%"}</span>
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full"
              style={{ width: `${(r.cost / max) * 100}%`, background: r.chart }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function ModelTable() {
  const { range } = useTimeRange()
  const rows = costByModel(range)
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            <th className="px-3 py-2 font-medium text-muted-foreground">Modelo</th>
            <th className="px-3 py-2 text-right font-medium text-muted-foreground">Ll.</th>
            <th className="px-3 py-2 text-right font-medium text-muted-foreground">Tokens</th>
            <th className="px-3 py-2 text-right font-medium text-muted-foreground">Costo</th>
            <th className="px-3 py-2 text-right font-medium text-muted-foreground">$/1k</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((r) => (
            <tr key={r.model} className="hover:bg-accent/40">
              <td className="px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{r.label}</span>
                  {r.untabulated && (
                    <span
                      className="inline-flex items-center gap-1 rounded border border-status-review/40 bg-status-review/10 px-1 py-0.5 text-[10px] font-medium text-status-review"
                      title="Modelo no tabulado en MODEL_PRICING → compute_cost devuelve 0 silencioso"
                    >
                      <TriangleAlert className="size-3" />
                      precio no tabulado
                    </span>
                  )}
                </div>
                <div className="num text-[11px] text-muted-foreground">{r.model}</div>
              </td>
              <td className="num px-3 py-2 text-right">{formatInt(r.calls)}</td>
              <td className="num px-3 py-2 text-right text-muted-foreground">
                {formatCompact(r.promptTokens + r.completionTokens)}
              </td>
              <td className="num px-3 py-2 text-right font-medium">
                {r.untabulated ? <span className="text-status-review">{formatUsd(0)}</span> : formatUsd(r.cost)}
              </td>
              <td className="num px-3 py-2 text-right text-muted-foreground" title={formatUsdFine(r.costPer1k)}>
                {r.untabulated ? "—" : formatUsd(r.costPer1k)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function CostBreakdown() {
  return (
    <Panel>
      <PanelHeader
        eyebrow="Desglose · costo LLM"
        title="A dónde va el gasto"
        sub="Por propósito y por modelo (Flash vs Pro)"
      />
      <PanelBody>
        <Stateful
          skeleton={<TableSkeleton rows={5} cols={4} />}
          empty={<EmptyState title="Sin gasto que desglosar" />}
        >
          <div className="grid gap-6 lg:grid-cols-2">
            <div>
              <div className="eyebrow mb-3">por propósito</div>
              <PurposeBars />
            </div>
            <div>
              <div className="eyebrow mb-3">por modelo</div>
              <ModelTable />
            </div>
          </div>
        </Stateful>
      </PanelBody>
    </Panel>
  )
}
