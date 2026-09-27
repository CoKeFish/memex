// Control del periodo: el mes (con flechas) y el largo de la ventana (mes, 3, 6, 12 meses o todo).
// Todo lo de abajo —resumen, tira, categorías, movimientos— se recorta a lo que diga esto.

import { ChevronLeft, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"
import { monthLongLabel, PERIOD_MODES, type Period, type PeriodMode } from "@/lib/finance-period"

export function PeriodPicker({
  period,
  months,
  onChange,
}: {
  period: Period
  /** Meses con datos, del más viejo al más nuevo: acotan las flechas. */
  months: string[]
  onChange: (p: Period) => void
}) {
  const first = months[0]
  const last = months[months.length - 1]
  const canPrev = !!first && period.month > first
  const canNext = !!last && period.month < last
  const step = (delta: number): void => {
    const i = months.indexOf(period.month)
    const next = i >= 0 ? months[i + delta] : undefined
    if (next) onChange({ ...period, month: next })
  }
  const todo = period.mode === "todo"

  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => step(-1)}
          disabled={todo || !canPrev}
          aria-label="Mes anterior"
          className="inline-flex size-8 items-center justify-center rounded-md border border-transparent text-muted-foreground hover:border-border hover:bg-muted hover:text-foreground disabled:opacity-30 disabled:hover:border-transparent disabled:hover:bg-transparent"
        >
          <ChevronLeft className="size-4" />
        </button>
        <h2 className="min-w-[11ch] text-center text-[15px] font-semibold tracking-tight">
          {todo ? "Todo" : monthLongLabel(period.month)}
        </h2>
        <button
          type="button"
          onClick={() => step(1)}
          disabled={todo || !canNext}
          aria-label="Mes siguiente"
          className="inline-flex size-8 items-center justify-center rounded-md border border-transparent text-muted-foreground hover:border-border hover:bg-muted hover:text-foreground disabled:opacity-30 disabled:hover:border-transparent disabled:hover:bg-transparent"
        >
          <ChevronRight className="size-4" />
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Largo del periodo">
        {PERIOD_MODES.map((m) => (
          <button
            key={m.key}
            type="button"
            onClick={() => onChange({ ...period, mode: m.key as PeriodMode })}
            aria-pressed={period.mode === m.key}
            title={m.hint}
            className={cn(
              "rounded-full border px-2.5 py-1 text-[12px] transition-colors",
              period.mode === m.key
                ? "border-brand bg-brand text-background"
                : "border-input text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            )}
          >
            {m.label}
          </button>
        ))}
      </div>
    </div>
  )
}
