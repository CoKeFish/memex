import { KpiCard } from "@/components/common/kpi-card"
import { Delta } from "@/components/common/stat"
import { formatMoney } from "@/lib/format"
import { CATEGORY_LABEL, financeKpis, financeMonthSummary } from "@/data"
import type { FinanceTransaction } from "@/types/domain"

export function FinanceKpis({ txns, currency }: { txns: FinanceTransaction[]; currency: string }) {
  const s = financeMonthSummary(txns, currency)
  // Las métricas de GASTO (delta vs mes anterior, categoría top) miran solo los egresos.
  const k = financeKpis(
    txns.filter((t) => t.direction === "egreso"),
    currency,
  )
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <KpiCard
        eyebrow={`Ingresos del mes · ${currency}`}
        value={<span className="text-status-ok">+{formatMoney(s.income, currency)}</span>}
        footer="lo que entró"
      />
      <KpiCard
        eyebrow={`Gastos del mes · ${currency}`}
        value={<span className="text-status-error">−{formatMoney(s.expense, currency)}</span>}
        delta={<Delta value={k.deltaPct} />}
        footer={`mes anterior ${formatMoney(k.lastMonth, currency)}`}
      />
      <KpiCard
        eyebrow={`Balance del mes · ${currency}`}
        value={
          <span className={s.balance >= 0 ? "text-status-ok" : "text-status-error"}>
            {s.balance >= 0 ? "+" : ""}
            {formatMoney(s.balance, currency)}
          </span>
        }
        footer="ingresos − gastos"
      />
      <KpiCard
        eyebrow="Categoría top de gasto"
        value={k.topCategory ? CATEGORY_LABEL[k.topCategory.category] : "—"}
        footer={k.topCategory ? formatMoney(k.topCategory.total, currency) : "sin gastos"}
      />
    </div>
  )
}
