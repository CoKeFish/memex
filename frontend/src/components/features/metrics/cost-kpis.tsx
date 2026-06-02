import { KpiCard } from "@/components/common/kpi-card"
import { Delta } from "@/components/common/stat"
import { formatCompact, formatInt, formatPct, formatUsd, formatUsdFine } from "@/lib/format"
import type { DailyCost, LlmKpis } from "@/data"

/** Variación vs el periodo anterior. `noBase` cuando el previo no es comparable (sin `since`, sin
 *  llamadas, o costo 0): evita un % gigante contra una base casi vacía. */
function deltaState(k: LlmKpis): { value: number | null; noBase: boolean } {
  if (k.prevCostUsd === null) return { value: null, noBase: false } // sin `since` → sin variación
  if (k.prevCalls === 0 || k.prevCostUsd <= 0) return { value: null, noBase: true } // previo sin base
  return { value: (k.costUsd - k.prevCostUsd) / k.prevCostUsd, noBase: false }
}

export function CostKpis({ kpis, daily }: { kpis: LlmKpis; daily: DailyCost[] }) {
  const totals = daily.map((d) => d.total)
  const delta = deltaState(kpis)
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <KpiCard
        eyebrow="Costo LLM · rango"
        value={formatUsd(kpis.costUsd)}
        delta={<Delta value={delta.value} noBase={delta.noBase} />}
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
      <KpiCard eyebrow="Costo / llamada" value={formatUsd(kpis.avgCostUsd)} footer="costo ÷ nº de llamadas" />
      <KpiCard
        eyebrow="Errores"
        value={formatInt(kpis.errors)}
        footer={kpis.calls ? `${formatPct(kpis.errors / kpis.calls, 0)} de las llamadas` : "—"}
      />
    </div>
  )
}
