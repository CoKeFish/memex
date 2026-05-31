import { KpiCard } from "@/components/common/kpi-card"
import { Delta } from "@/components/common/stat"
import { formatInt, formatMoney } from "@/lib/format"
import { CATEGORY_LABEL, financeKpis } from "@/data"

export function FinanceKpis({ currency }: { currency: string }) {
  const k = financeKpis(currency)
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <KpiCard
        eyebrow={`Gasto del mes · ${currency}`}
        value={formatMoney(k.thisMonth, currency)}
        delta={<Delta value={k.deltaPct} />}
        accent
        footer={`mes anterior ${formatMoney(k.lastMonth, currency)}`}
      />
      <KpiCard eyebrow="Movimientos del mes" value={formatInt(k.count)} footer="gastos registrados" />
      <KpiCard eyebrow="Ticket promedio" value={formatMoney(k.avg, currency)} footer="por movimiento" />
      <KpiCard
        eyebrow="Categoría top"
        value={k.topCategory ? CATEGORY_LABEL[k.topCategory.category] : "—"}
        footer={k.topCategory ? formatMoney(k.topCategory.total, currency) : "sin gastos"}
      />
    </div>
  )
}
