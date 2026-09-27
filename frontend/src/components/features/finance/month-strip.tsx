// Tira de meses: el control principal de la vista de finanzas. Cada columna es un mes con dos
// barras finas —lo que salió y lo que entró— y ES el navegador: al hacer clic, el periodo salta a
// ese mes. Así «ver por mes» y «ver la tendencia» son el mismo gesto, en vez de un gráfico decorativo
// arriba y un selector aparte.
//
// Sin librería de gráficos: son dos divs con altura porcentual. Da control total del estado activo,
// del foco de teclado y de los rótulos, y no arrastra recharts para doce barras.

import { cn } from "@/lib/utils"
import { compactAmount, money } from "@/lib/money"
import { monthLongLabel, type MonthBar } from "@/lib/finance-period"

export function MonthStrip({
  series,
  selected,
  currency,
  onSelect,
  className,
}: {
  series: MonthBar[]
  /** Meses del periodo: se resaltan todos (con «6 meses» la tira muestra cuáles entran en la
   * cifra grande); el resto se atenúa. */
  selected: string[]
  currency: string
  onSelect: (month: string) => void
  className?: string
}) {
  const max = Math.max(1, ...series.map((m) => Math.max(m.expense, m.income)))
  const activos = new Set(selected)
  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex items-end gap-px" style={{ height: 84 }} role="group" aria-label="Meses">
        {series.map((m) => {
          const on = activos.has(m.key)
          const nada = m.count === 0
          return (
            <button
              key={m.key}
              type="button"
              onClick={() => onSelect(m.key)}
              aria-pressed={on}
              title={
                nada
                  ? `${monthLongLabel(m.key)}: sin movimientos`
                  : `${monthLongLabel(m.key)}: salió ${money(m.expense, currency)}, entró ${money(m.income, currency)}`
              }
              className={cn(
                "group flex h-full min-w-0 flex-1 flex-col justify-end rounded-sm px-px pt-1 outline-none transition-colors",
                "focus-visible:ring-2 focus-visible:ring-ring",
                on ? "bg-muted/70" : "hover:bg-muted/40",
              )}
            >
              <span className="flex h-full items-end justify-center gap-[2px]">
                <span
                  className={cn(
                    "w-[5px] rounded-t-[2px] bg-brand transition-opacity",
                    on ? "opacity-100" : "opacity-45 group-hover:opacity-75",
                  )}
                  style={{ height: `${Math.max(m.expense > 0 ? 3 : 0, (m.expense / max) * 100)}%` }}
                />
                <span
                  className={cn(
                    "w-[5px] rounded-t-[2px] bg-status-ok transition-opacity",
                    on ? "opacity-100" : "opacity-45 group-hover:opacity-75",
                  )}
                  style={{ height: `${Math.max(m.income > 0 ? 3 : 0, (m.income / max) * 100)}%` }}
                />
              </span>
            </button>
          )
        })}
      </div>
      <div className="mt-1 flex gap-px">
        {series.map((m) => (
          <span
            key={m.key}
            className={cn(
              "num min-w-0 flex-1 truncate text-center text-[10px]",
              activos.has(m.key) ? "text-foreground" : "text-muted-foreground/70",
            )}
          >
            {m.label}
          </span>
        ))}
      </div>
      <p className="mt-2 flex items-center justify-center gap-4 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-[2px] bg-brand" /> salió
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-[2px] bg-status-ok" /> entró
        </span>
        <span className="num" title="La barra más alta de la tira">
          más alto {compactAmount(max)}
        </span>
      </p>
    </div>
  )
}
