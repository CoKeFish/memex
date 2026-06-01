import { TriangleAlert } from "lucide-react"
import { EmptyState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatCompact, formatInt, formatPct, formatUsd, formatUsdFine } from "@/lib/format"
import { moduleChart, moduleLabel } from "@/lib/metrics"
import type { ModelCost, ModuleCost } from "@/data"

function ModuleBars({ rows }: { rows: ModuleCost[] }) {
  const max = Math.max(...rows.map((r) => r.costUsd), 0.0001)
  const total = rows.reduce((a, r) => a + r.costUsd, 0)
  return (
    <div className="space-y-3">
      {rows.map((r) => (
        <div key={r.module}>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="flex items-center gap-2">
              <span className="size-2 rounded-[2px]" style={{ background: moduleChart(r.module) }} />
              <span className="font-medium">{moduleLabel(r.module)}</span>
              <span className="num text-muted-foreground">{formatInt(r.calls)} ll.</span>
            </span>
            <span className="num font-medium">
              {formatUsd(r.costUsd)}
              <span className="ml-1.5 text-muted-foreground">{total ? formatPct(r.costUsd / total, 0) : "0%"}</span>
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full"
              style={{ width: `${(r.costUsd / max) * 100}%`, background: moduleChart(r.module) }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function ModelTable({ rows }: { rows: ModelCost[] }) {
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
          {rows.map((r) => {
            const tokens = r.promptTokens + r.completionTokens
            const per1k = tokens ? (r.costUsd / tokens) * 1000 : 0
            return (
              <tr key={r.model} className="hover:bg-accent/40">
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="num font-medium">{r.model}</span>
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
                </td>
                <td className="num px-3 py-2 text-right">{formatInt(r.calls)}</td>
                <td className="num px-3 py-2 text-right text-muted-foreground">{formatCompact(tokens)}</td>
                <td className="num px-3 py-2 text-right font-medium">
                  {r.untabulated ? <span className="text-status-review">{formatUsd(0)}</span> : formatUsd(r.costUsd)}
                </td>
                <td className="num px-3 py-2 text-right text-muted-foreground" title={formatUsdFine(per1k)}>
                  {r.untabulated ? "—" : formatUsd(per1k)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function CostBreakdown({ byModule, byModel }: { byModule: ModuleCost[]; byModel: ModelCost[] }) {
  return (
    <Panel>
      <PanelHeader eyebrow="Desglose · costo LLM" title="A dónde va el gasto" sub="Por módulo y por modelo" />
      <PanelBody>
        {byModule.length === 0 ? (
          <EmptyState title="Sin gasto que desglosar" />
        ) : (
          <div className="grid gap-6 lg:grid-cols-2">
            <div>
              <div className="eyebrow mb-3">por módulo</div>
              <ModuleBars rows={byModule} />
            </div>
            <div>
              <div className="eyebrow mb-3">por modelo</div>
              <ModelTable rows={byModel} />
            </div>
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
