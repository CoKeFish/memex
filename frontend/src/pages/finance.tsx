import { useState } from "react"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { FinanceKpis } from "@/components/features/finance/finance-kpis"
import { CategoryBreakdown, MonthlyTrend, TopMerchants } from "@/components/features/finance/finance-charts"
import { ExpenseTable } from "@/components/features/finance/expense-table"
import { fetchFinanceExpenses, financeCurrencies } from "@/data"
import { useAsync } from "@/lib/use-async"
import { formatMoney } from "@/lib/format"
import type { FinanceExpense } from "@/types/domain"

export function FinancePage() {
  const { data, loading, error, reload } = useAsync<FinanceExpense[]>(() => fetchFinanceExpenses(), [])
  const expenses = data ?? []
  const currencies = financeCurrencies(expenses)
  const [picked, setPicked] = useState<string | null>(null)
  // Moneda activa: la elegida si sigue presente tras cargar; si no, la más usada (o MXN de respaldo).
  const currency = picked && currencies.includes(picked) ? picked : currencies[0] ?? "MXN"

  const others = currencies
    .filter((c) => c !== currency)
    .map((c) => ({ currency: c, total: expenses.filter((e) => e.currency === c).reduce((a, e) => a + e.amount, 0) }))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · finance"
        title="Finanzas"
        description="Todo lo que extrajo el módulo finance, como una app de finanzas personales: gasto del mes, tendencia, tipos de gasto, top comercios y el detalle de cada movimiento con su evidencia y mensaje de origen."
        actions={
          currencies.length > 0 ? (
            <Select value={currency} onValueChange={setPicked}>
              <SelectTrigger className="h-8 w-auto min-w-[88px] text-xs" aria-label="Moneda">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {currencies.map((c) => (
                  <SelectItem key={c} value={c} className="text-xs">{c}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : undefined
        }
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando finanzas…
        </div>
      ) : expenses.length === 0 ? (
        <EmptyState title="Sin gastos" hint="El módulo finance aún no extrajo nada." />
      ) : (
        <>
          <FinanceKpis expenses={expenses} currency={currency} />
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
            <MonthlyTrend expenses={expenses} currency={currency} />
            <CategoryBreakdown expenses={expenses} currency={currency} />
          </div>
          <div className="grid gap-5 xl:grid-cols-[1fr_1.4fr]">
            <TopMerchants expenses={expenses} currency={currency} />
            <ExpenseTable expenses={expenses} currency={currency} />
          </div>
        </>
      )}
    </div>
  )
}
