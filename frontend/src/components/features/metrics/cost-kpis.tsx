import { KpiCard } from "@/components/common/kpi-card"
import { Delta } from "@/components/common/stat"
import { formatCompact, formatInt, formatPct, formatUsd, formatUsdFine } from "@/lib/format"
import type { DailyCost, LlmKpis } from "@/data"

/** Variación vs el periodo anterior (solo si hay base previa > 0). */
function deltaPct(k: LlmKpis): number | null {
  if (k.prevCostUsd === null || k.prevCostUsd <= 0) return null
  return (k.costUsd - k.prevCostUsd) / k.prevCostUsd
}

export function CostKpis({ kpis, daily }: { kpis: LlmKpis; daily: DailyCost[] }) {
  const totals = daily.map((d) => d.total)
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <KpiCard
        eyebrow="Costo LLM · rango"
        value={formatUsd(kpis.costUsd)}
        delta={<Delta value={deltaPct(kpis)} />}
        sparkData={totals}
        accent
        footer={`exacto ${formatUsdFine(kpis.costUsd)}`}
      />
      <KpiCard eyebrow="Llamadas" value={formatInt(kpis.calls)} footer="filas en llm_calls" />
      <KpiCard
        eyebrow="Tokens"
        value={formatCompact(kpis.promptTokens + kpis.completionTokens)}
        footer="prompt + completion"
      />
      <KpiCard
        eyebrow="Cache-hit"
        value={formatPct(kpis.cacheHitRatio, 0)}
        footer={`${formatCompact(kpis.cacheHitTokens)} tok desde cache`}
      />
      <KpiCard eyebrow="Costo / llamada" value={formatUsd(kpis.avgCostUsd)} footer="promedio del rango" />
      <KpiCard
        eyebrow="Errores"
        value={formatInt(kpis.errors)}
        footer={kpis.calls ? `${formatPct(kpis.errors / kpis.calls, 0)} de las llamadas` : "—"}
      />
    </div>
  )
}
