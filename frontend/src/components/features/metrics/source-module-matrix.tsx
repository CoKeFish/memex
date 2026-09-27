import { useMemo } from "react"
import { cn } from "@/lib/utils"
import { EmptyState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatUsd } from "@/lib/format"
import { moduleLabel } from "@/lib/metrics"
import type { SourceModuleCost } from "@/data"

interface Row {
  sourceName: string
  byModule: Record<string, number>
  total: number
}

/** Matriz de costo fuente (filas) × módulo (columnas): dónde gasta cada módulo, por fuente. */
export function SourceModuleMatrix({
  bySourceModule,
  modules,
}: {
  bySourceModule: SourceModuleCost[]
  modules: string[]
}) {
  const { rows, colTotals, grand } = useMemo(() => {
    const map = new Map<string, Row>()
    const colTotals: Record<string, number> = {}
    let grand = 0
    for (const c of bySourceModule) {
      const row = map.get(c.sourceName) ?? { sourceName: c.sourceName, byModule: {}, total: 0 }
      row.byModule[c.module] = (row.byModule[c.module] ?? 0) + c.costUsd
      row.total += c.costUsd
      map.set(c.sourceName, row)
      colTotals[c.module] = (colTotals[c.module] ?? 0) + c.costUsd
      grand += c.costUsd
    }
    const rows = [...map.values()].sort((a, b) => b.total - a.total)
    return { rows, colTotals, grand }
  }, [bySourceModule])

  // Intensidad de la celda relativa al máximo (heatmap sutil).
  const cellMax = Math.max(...bySourceModule.map((c) => c.costUsd), 0.0001)

  return (
    <Panel>
      <PanelHeader
        eyebrow="Cruce · fuente × módulo"
        title="Matriz de gasto"
        sub="Costo por fuente y módulo — ubica qué etapa gasta en qué ingestor"
      />
      <PanelBody className="p-0">
        {rows.length === 0 ? (
          <EmptyState title="Sin datos para cruzar" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/30 text-left">
                  <th className="px-3 py-2 font-medium text-muted-foreground">Fuente</th>
                  {modules.map((m) => (
                    <th key={m} className="px-3 py-2 text-right font-medium text-muted-foreground">
                      {moduleLabel(m)}
                    </th>
                  ))}
                  <th className="px-3 py-2 text-right font-medium text-foreground">Total</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => (
                  <tr key={r.sourceName} className="hover:bg-accent/30">
                    <td className={cn("px-3 py-2", r.sourceName.startsWith("(") && "italic text-muted-foreground")}>
                      {r.sourceName}
                    </td>
                    {modules.map((m) => {
                      const v = r.byModule[m] ?? 0
                      return (
                        <td
                          key={m}
                          className="num px-3 py-2 text-right"
                          style={v > 0 ? { background: `color-mix(in oklch, var(--brand) ${Math.round((v / cellMax) * 60)}%, transparent)` } : undefined}
                        >
                          {v > 0 ? formatUsd(v) : <span className="text-muted-foreground/40">—</span>}
                        </td>
                      )
                    })}
                    <td className="num px-3 py-2 text-right font-medium">
                      {r.total > 0 ? formatUsd(r.total) : <span className="text-muted-foreground/40">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-border bg-muted/30 font-medium">
                  <td className="px-3 py-2 text-muted-foreground">Total</td>
                  {modules.map((m) => (
                    <td key={m} className="num px-3 py-2 text-right">
                      {colTotals[m] ? formatUsd(colTotals[m]) : <span className="text-muted-foreground/40">—</span>}
                    </td>
                  ))}
                  <td className="num px-3 py-2 text-right">
                    {grand > 0 ? formatUsd(grand) : <span className="text-muted-foreground/40">—</span>}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
