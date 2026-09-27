// En qué se fue la plata del periodo. Barras de datos dentro de la propia fila (no un gráfico
// aparte): la barra es el fondo, el nombre y el monto van encima, y la fila ENTERA es un filtro —
// al hacer clic, la lista de movimientos de al lado se queda con esa categoría. Un anillo marca la
// que está filtrando.
//
// Sin torta: con ocho categorías y una que se lleva dos tercios, una torta no se puede leer ni
// clicar bien; una lista ordenada responde «¿en qué se fue?» de un vistazo.

import { cn } from "@/lib/utils"
import { money } from "@/lib/money"
import type { CatAgg } from "@/lib/finance"

export function CategoryBars({
  rows,
  currency,
  active,
  onPick,
}: {
  rows: CatAgg[]
  currency: string
  /** Categoría filtrada ("all" = ninguna). */
  active: string
  onPick: (category: string) => void
}) {
  const total = rows.reduce((a, r) => a + r.total, 0)
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">Sin gastos en este periodo.</p>
  }
  const max = Math.max(...rows.map((r) => r.total), 1)
  return (
    <ul className="space-y-1">
      {rows.map((r) => {
        const on = active === r.category
        const pct = total > 0 ? Math.round((r.total / total) * 100) : 0
        return (
          <li key={r.category}>
            <button
              type="button"
              onClick={() => onPick(on ? "all" : r.category)}
              aria-pressed={on}
              title={
                on
                  ? "Quitar el filtro"
                  : `Ver solo los movimientos de ${r.label.toLowerCase()} en este periodo`
              }
              className={cn(
                "relative flex w-full items-center gap-3 overflow-hidden rounded-md px-2.5 py-1.5 text-left outline-none",
                "hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring",
                on && "ring-1 ring-brand",
              )}
            >
              <span
                aria-hidden
                className="absolute inset-y-0 left-0 rounded-md opacity-[0.22]"
                style={{ width: `${(r.total / max) * 100}%`, background: r.chart }}
              />
              <span className="relative z-10 flex min-w-0 flex-1 items-center gap-2">
                <span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: r.chart }} />
                <span className="truncate text-sm">{r.label}</span>
                <span className="num shrink-0 text-[11px] text-muted-foreground">{r.count}</span>
              </span>
              <span className="relative z-10 shrink-0 text-right">
                <span className="num text-sm">{money(r.total, currency)}</span>
                <span className="num ml-2 text-[11px] text-muted-foreground">{pct}%</span>
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
