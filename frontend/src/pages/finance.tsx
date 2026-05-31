import { useState } from "react"
import { PageHeader } from "@/components/common/page-header"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { FinanceKpis } from "@/components/features/finance/finance-kpis"
import { CategoryBreakdown, MonthlyTrend, TopMerchants } from "@/components/features/finance/finance-charts"
import { ExpenseTable } from "@/components/features/finance/expense-table"
import { financeCurrencies, getFinanceExpenses } from "@/data"
import { formatMoney } from "@/lib/format"

export function FinancePage() {
  const currencies = financeCurrencies()
  const [currency, setCurrency] = useState(currencies[0] ?? "MXN")

  const all = getFinanceExpenses()
  const others = currencies
    .filter((c) => c !== currency)
    .map((c) => ({ currency: c, total: all.filter((e) => e.currency === c).reduce((a, e) => a + e.amount, 0) }))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · finance"
        title="Finanzas"
        description="Todo lo que extrajo el módulo finance, como una app de finanzas personales: gasto del mes, tendencia, tipos de gasto, top comercios y el detalle de cada movimiento con su evidencia y mensaje de origen."
        actions={
          <Select value={currency} onValueChange={setCurrency}>
            <SelectTrigger className="h-8 w-auto min-w-[88px] text-xs" aria-label="Moneda">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {currencies.map((c) => (
                <SelectItem key={c} value={c} className="text-xs">{c}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      <FinanceKpis currency={currency} />
      {others.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Otras monedas (todo el periodo):{" "}
          {others.map((o) => (
            <span key={o.currency} className="num">
              {o.currency} {formatMoney(o.total, o.currency)}{" "}
            </span>
          ))}
          · los montos no se suman entre monedas (sin tipo de cambio).
        </p>
      )}
      <div className="grid gap-5 xl:grid-cols-2">
        <MonthlyTrend currency={currency} />
        <CategoryBreakdown currency={currency} />
      </div>
      <div className="grid gap-5 xl:grid-cols-[1fr_1.4fr]">
        <TopMerchants currency={currency} />
        <ExpenseTable currency={currency} />
      </div>
    </div>
  )
}
