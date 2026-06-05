import { useState } from "react"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { FinanceKpis } from "@/components/features/finance/finance-kpis"
import { CategoryBreakdown, MonthlyTrend, TopMerchants } from "@/components/features/finance/finance-charts"
import { MovementsTable } from "@/components/features/finance/movements-table"
import { FinanceActivity } from "@/components/features/finance/finance-activity"
import { fetchFinanceTransactions, financeCurrencies } from "@/data"
import { useAsync } from "@/lib/use-async"
import { formatMoney } from "@/lib/format"
import type { FinanceTransaction } from "@/types/domain"

export function FinancePage() {
  const { data, loading, error, reload } = useAsync<FinanceTransaction[]>(() => fetchFinanceTransactions(), [])
  const txns = data ?? []
  // Los gráficos de GASTO (tendencia, tipos, top comercios) operan solo sobre egresos; el resumen y la
  // tabla de movimientos miran todas las transacciones.
  const egresos = txns.filter((t) => t.direction === "egreso")
  const currencies = financeCurrencies(txns)
  const [picked, setPicked] = useState<string | null>(null)
  // Moneda activa: la elegida si sigue presente tras cargar; si no, la más usada (o MXN de respaldo).
  const currency = picked && currencies.includes(picked) ? picked : currencies[0] ?? "MXN"

  // Neto (ingresos − gastos) por cada otra moneda: nunca se suman entre monedas (sin tipo de cambio).
  const others = currencies
    .filter((c) => c !== currency)
    .map((c) => ({
      currency: c,
      total: txns
        .filter((t) => t.currency === c)
        .reduce((a, t) => a + (t.direction === "egreso" ? -t.amount : t.amount), 0),
    }))

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · finance"
        title="Finanzas"
        description="Todo lo que extrajo el módulo finance, como una app de finanzas personales: ingresos, gastos y balance del mes; tendencia y tipos de gasto, top comercios, y el detalle de cada movimiento con su evidencia y mensaje de origen."
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
      ) : (
        <>
          {txns.length === 0 ? (
            <EmptyState
              title="Sin movimientos"
              hint="El módulo finance aún no extrajo movimientos. Mirá la actividad del módulo abajo para ver si corrió, qué procesó y si hubo errores."
            />
          ) : (
            <>
              <FinanceKpis txns={txns} currency={currency} />
              {others.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Otras monedas (neto del periodo):{" "}
                  {others.map((o) => (
                    <span key={o.currency} className="num">
                      {o.currency} {formatMoney(o.total, o.currency)}{" "}
                    </span>
                  ))}
                  · los montos no se suman entre monedas (sin tipo de cambio).
                </p>
              )}
              <div className="grid gap-5 xl:grid-cols-2">
                <MonthlyTrend expenses={egresos} currency={currency} />
                <CategoryBreakdown expenses={egresos} currency={currency} />
              </div>
              <div className="grid gap-5 xl:grid-cols-[1fr_1.4fr]">
                <TopMerchants expenses={egresos} currency={currency} />
                <MovementsTable txns={txns} currency={currency} />
              </div>
            </>
          )}
          {/* Siempre visible: cuando hay 0 movimientos es justo lo que explica el "por qué". */}
          <FinanceActivity />
        </>
      )}
    </div>
  )
}
