import { KpiCard } from "@/components/common/kpi-card"
import { CardsSkeleton, Stateful } from "@/components/common/data-state"
import { EmptyState } from "@/components/common/data-state"
import { Delta } from "@/components/common/stat"
import { formatCompact, formatInt, formatUsd, formatUsdFine } from "@/lib/format"
import { costDaily, costKpis } from "@/lib/selectors"
import { useTimeRange } from "@/state/time-range"

export function CostKpis() {
  const { range } = useTimeRange()
  const k = costKpis(range)
  const daily = costDaily(range)
  const totals = daily.map((d) => d.total)

  return (
    <Stateful
      skeleton={<CardsSkeleton count={4} />}
      empty={<EmptyState title="Sin llamadas LLM en el rango" hint="Ningún worker corrió todavía en esta ventana." />}
      errorDetail="HTTP 500 — GET /metrics/llm-cost falló"
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          eyebrow="Costo LLM · rango"
          value={formatUsd(k.cost)}
          delta={<Delta value={k.deltaPct} />}
          sparkData={totals}
          accent
          footer={`exacto ${formatUsdFine(k.cost)}`}
        />
        <KpiCard eyebrow="Llamadas" value={formatInt(k.calls)} footer="suma sobre llm_calls" />
        <KpiCard eyebrow="Tokens" value={formatCompact(k.tokens)} footer="prompt + completion" />
        <KpiCard eyebrow="Costo / llamada" value={formatUsd(k.avgCost)} footer="promedio del rango" />
      </div>
    </Stateful>
  )
}
