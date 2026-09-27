// Cabecera del periodo. Una sola cifra grande —lo gastado— y debajo los datos que la explican, en
// frases cortas. Nada de tres números del mismo tamaño compitiendo (gasto, entrada y balance decían
// casi lo mismo cuando no entró plata) ni de comparaciones en jerga: la referencia se nombra entera
// («en los 3 meses anteriores, al día 14, habías gastado X») y la diferencia va en pesos, que es lo
// que uno entiende, con el porcentaje como apoyo.

import { cn } from "@/lib/utils"
import { money, signedMoney } from "@/lib/money"
import { monthInSentence, type Coverage, type MonthBar, type Pace, type Summary } from "@/lib/finance-period"
import { MonthStrip } from "./month-strip"

/** Cómo va el mes EN CURSO: dónde estás hoy y dónde estabas el mismo día de los meses anteriores.
 * Sin esto, un mes por la mitad siempre parece barato. */
function Ritmo({ pace, currency }: { pace: Pace; currency: string }) {
  const dias = `Van ${pace.day} de ${pace.days} días.`
  if (pace.basis === 0) {
    return (
      <p className="text-xs leading-relaxed text-muted-foreground">
        {dias} Todavía no hay meses anteriores con qué comparar.
      </p>
    )
  }
  const delta = pace.spent - pace.average
  const pct = pace.average > 0 ? Math.round(Math.abs(delta / pace.average) * 100) : null
  const mas = delta > 0
  const meses = pace.basis === 1 ? "el mes anterior" : `los ${pace.basis} meses anteriores`
  return (
    <p className="text-xs leading-relaxed text-muted-foreground">
      {dias} En {meses}, al día {pace.day}, habías gastado{" "}
      <span className="num text-foreground">{money(pace.average, currency)}</span>.{" "}
      <span className={cn("font-medium", mas ? "text-status-error" : "text-status-ok")}>
        Vas <span className="num">{money(Math.abs(delta), currency)}</span> {mas ? "más" : "menos"}
      </span>
      {pct !== null && <span className="num"> ({pct}%)</span>}.
    </p>
  )
}

export function FinanceSummary({
  summary,
  series,
  selectedMonths,
  currency,
  pace,
  periodLabel,
  coverage,
  today,
  onSelectMonth,
}: {
  summary: Summary
  series: MonthBar[]
  /** Los meses que entran en la cifra: la tira los resalta. */
  selectedMonths: string[]
  currency: string
  pace: Pace | null
  /** «septiembre», «los últimos 6 meses»: completa «Gastos de …». */
  periodLabel: string
  /** Qué meses del periodo no tienen el extracto del banco. */
  coverage: Coverage
  today: Date
  onSelectMonth: (month: string) => void
}) {
  // El promedio se divide por los meses del PERIODO, no por los de la tira (que siempre son 12).
  const meses = selectedMonths.length
  const promedio = meses > 1 ? summary.expense / meses : null
  // Comparar el mes en curso (sin extracto, porque el banco lo emite al cerrar) contra meses
  // que SÍ lo tienen daría un «vas por debajo» falso: ahí se calla y se explica por qué.
  const comparaJusto = !(coverage.comparable && coverage.currentMonth)
  const faltan = coverage.comparable ? coverage.missing : []

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] lg:items-center">
        <div className="min-w-0">
          <h2 className="text-xs text-muted-foreground">Gastos de {periodLabel}</h2>
          <p className="kpi mt-1 text-[34px] leading-none tracking-tight">
            {money(summary.expense, currency)}
          </p>

          <p className="mt-2 text-xs text-muted-foreground">
            <span className="num">{summary.count}</span> movimientos
            {promedio !== null && (
              <>
                {", "}
                <span className="num">{money(promedio, currency)}</span> por mes
              </>
            )}
            {summary.refunded > 0 && (
              <>
                {". Te devolvieron "}
                <span className="num">{money(summary.refunded, currency)}</span>, ya descontados
              </>
            )}
            .
          </p>

          <p className="mt-1 text-xs text-muted-foreground">
            {summary.income > 0 ? (
              <>
                Entró <span className="num text-status-ok">{money(summary.income, currency)}</span>,
                así que quedás en{" "}
                <span
                  className={cn(
                    "num font-medium",
                    summary.balance >= 0 ? "text-status-ok" : "text-foreground",
                  )}
                >
                  {signedMoney(summary.balance, currency)}
                </span>
                .
              </>
            ) : (
              "No entró plata en este periodo."
            )}
          </p>

          {pace && comparaJusto && (
            <div className="mt-2 border-t border-border pt-2">
              <Ritmo pace={pace} currency={currency} />
            </div>
          )}

          {(faltan.length > 0 || (coverage.currentMonth && coverage.comparable)) && (
            <p className="mt-3 border-l-2 border-status-review pl-3 text-xs leading-relaxed text-muted-foreground">
              {faltan.length > 0 ? (
                <>
                  Falta el extracto de{" "}
                  <span className="text-foreground">
                    {faltan.map((m) => monthInSentence(m, today)).join(", ")}
                  </span>
                  : de {faltan.length === 1 ? "ese mes" : "esos meses"} solo están los cobros que
                  llegaron por correo, así que esta cifra queda corta. Subilo con «Subir extracto».
                </>
              ) : (
                <>
                  {pace && `Van ${pace.day} de ${pace.days} días. `}
                  El extracto de este mes sale cuando el banco lo cierre; por ahora solo están los
                  cobros que llegaron por correo, así que la cifra va a subir.
                </>
              )}
            </p>
          )}
        </div>

        <MonthStrip
          series={series}
          selected={selectedMonths}
          currency={currency}
          onSelect={onSelectMonth}
        />
      </div>

      {summary.unsettled > 0 && (
        <p className="mt-3 border-t border-border pt-2.5 text-[11px] text-muted-foreground">
          {summary.unsettled}{" "}
          {summary.unsettled === 1
            ? "cobro no se movió (falló o quedó pendiente)"
            : "cobros no se movieron (fallaron o quedaron pendientes)"}
          : aparecen en la lista y no suman.
        </p>
      )}
    </section>
  )
}
