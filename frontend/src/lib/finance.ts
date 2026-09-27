// Catálogo de categorías de gasto y monedas presentes: lo compartido entre la vista de finanzas
// (`lib/finance-period.ts`, que hace el recorte por periodo y los cortes) y cualquier otra que
// muestre un movimiento. Las categorías son las del backend (`FINANCE_CATEGORIES`); el color sale
// de los tokens del tema, no de hexadecimales sueltos.

import type { ExpenseCategory, FinanceTransaction } from "@/types/domain"

export const CATEGORIES: { key: ExpenseCategory; label: string; chart: string }[] = [
  { key: "comida", label: "Comida", chart: "var(--chart-1)" },
  { key: "transporte", label: "Transporte", chart: "var(--chart-2)" },
  { key: "software", label: "Software", chart: "var(--chart-3)" },
  { key: "servicios", label: "Servicios", chart: "var(--chart-4)" },
  { key: "entretenimiento", label: "Entretenimiento", chart: "var(--chart-5)" },
  { key: "educacion", label: "Educación", chart: "var(--origin-inbox)" },
  { key: "salud", label: "Salud", chart: "var(--status-ok)" },
  { key: "compras", label: "Compras", chart: "var(--chart-6)" },
  { key: "transferencia", label: "Transferencia", chart: "var(--chart-7)" },
  { key: "traspaso", label: "Traspaso", chart: "var(--status-pending)" },
  { key: "ingreso", label: "Ingreso", chart: "var(--origin-provider)" },
  { key: "rendimientos", label: "Rendimientos", chart: "var(--origin-module)" },
  // «Otros» es solo lo que no se sabe qué es (las transferencias y traspasos ya tienen rubro);
  // con el gris de `--status-filtered` la barra no se veía sobre la tarjeta.
  { key: "otros", label: "Otros", chart: "var(--muted-foreground)" },
]

export const CATEGORY_LABEL = Object.fromEntries(
  CATEGORIES.map((c) => [c.key, c.label]),
) as Record<ExpenseCategory, string>

export const CATEGORY_CHART = Object.fromEntries(
  CATEGORIES.map((c) => [c.key, c.chart]),
) as Record<ExpenseCategory, string>

/** Un corte por categoría (lo arma `finance-period.byCategory`). */
export interface CatAgg {
  category: ExpenseCategory
  label: string
  chart: string
  total: number
  count: number
}

/** Monedas presentes en los movimientos, la más usada primero: nunca se suman entre sí. */
export function financeCurrencies(txns: FinanceTransaction[]): string[] {
  const count = new Map<string, number>()
  for (const t of txns) count.set(t.currency, (count.get(t.currency) ?? 0) + 1)
  return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c)
}
