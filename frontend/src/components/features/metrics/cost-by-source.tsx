import { EmptyState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatCompact, formatInt, formatUsd, pctShare } from "@/lib/format"
import type { SourceCost } from "@/data"

//: 8 hues distintos (antes 7, con chart-5 y origin-module casi idénticos → la 8va fuente reusaba
//: color). chart-6/7 son tokens nuevos del tema; origin-provider cierra la paleta.
const PALETTE = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--origin-provider)",
]

/** Costo por fuente (ingestor). Las llamadas sin source caen en los pseudo "(calendar)"/"(sin
 *  source)" que el backend etiqueta para que ese gasto se VEA, no se pierda. */
export function CostBySource({ bySource }: { bySource: SourceCost[] }) {
  const max = Math.max(...bySource.map((r) => r.costUsd), 0.0001)
  const total = bySource.reduce((a, r) => a + r.costUsd, 0)
  return (
    <Panel>
      <PanelHeader
        eyebrow="Desglose · por fuente"
        title="Gasto por fuente"
        sub="Costo LLM atribuido a cada ingestor (llm_calls.source_id)"
      />
      <PanelBody>
        {bySource.length === 0 ? (
          <EmptyState title="Sin gasto por fuente" />
        ) : (
          <div className="space-y-3">
            {bySource.map((r, i) => {
              const color = PALETTE[i % PALETTE.length]
              const pseudo = r.sourceId === null
              return (
                <div key={r.sourceName}>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="flex items-center gap-2">
                      <span className="size-2 rounded-[2px]" style={{ background: color }} />
                      <span className={pseudo ? "font-medium italic text-muted-foreground" : "font-medium"}>
                        {r.sourceName}
                      </span>
                      <span className="num text-muted-foreground">
                        {formatInt(r.calls)} ll. · {formatCompact(r.tokens)} tok
                      </span>
                    </span>
                    <span className="num font-medium">
                      {formatUsd(r.costUsd)}
                      <span className="ml-1.5 text-muted-foreground">{pctShare(r.costUsd, total)}</span>
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full" style={{ width: `${(r.costUsd / max) * 100}%`, background: color }} />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
