// Agregaciones puras del módulo finance sobre una lista de gastos (`FinanceExpense[]`) que el caller
// trae de la API (`@/data` → `fetchFinanceExpenses`). Sin estado ni mocks: el page hace UN fetch y
// pasa los gastos a estas funciones. Los buckets de mes son relativos a la fecha REAL (hoy), no al
// `NOW` fijo de los mocks.
import { monthKey, monthLabel } from "@/lib/format"
import type { ExpenseCategory, FinanceExpense } from "@/types/domain"

export const CATEGORIES: { key: ExpenseCategory; label: string; chart: string }[] = [
  { key: "comida", label: "Comida", chart: "var(--chart-1)" },
  { key: "transporte", label: "Transporte", chart: "var(--chart-2)" },
  { key: "software", label: "Software", chart: "var(--chart-3)" },
  { key: "servicios", label: "Servicios", chart: "var(--chart-4)" },
  { key: "entretenimiento", label: "Entretenimiento", chart: "var(--chart-5)" },
  { key: "educacion", label: "Educación", chart: "var(--origin-inbox)" },
  { key: "salud", label: "Salud", chart: "var(--status-ok)" },
  { key: "otros", label: "Otros", chart: "var(--status-filtered)" },
]
export const CATEGORY_LABEL = Object.fromEntries(CATEGORIES.map((c) => [c.key, c.label])) as Record<ExpenseCategory, string>
export const CATEGORY_CHART = Object.fromEntries(CATEGORIES.map((c) => [c.key, c.chart])) as Record<ExpenseCategory, string>

/** Monedas presentes, la más usada primero. */
export function financeCurrencies(expenses: FinanceExpense[]): string[] {
  const count = new Map<string, number>()
  for (const e of expenses) count.set(e.currency, (count.get(e.currency) ?? 0) + 1)
  return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([c]) => c)
}

function inCurrency(expenses: FinanceExpense[], currency: string): FinanceExpense[] {
  return expenses.filter((e) => e.currency === currency)
}

export interface MonthPoint {
  key: string
  label: string
  total: number
  byCategory: Record<ExpenseCategory, number>
}

function emptyCats(): Record<ExpenseCategory, number> {
  return { comida: 0, transporte: 0, software: 0, servicios: 0, educacion: 0, salud: 0, entretenimiento: 0, otros: 0 }
}

export function financeByMonth(expenses: FinanceExpense[], currency: string, months = 5): MonthPoint[] {
  const buckets: MonthPoint[] = []
  const today = new Date()
  const base = new Date(today.getFullYear(), today.getMonth(), 1)
  for (let i = months - 1; i >= 0; i--) {
    const d = new Date(base.getFullYear(), base.getMonth() - i, 1)
    buckets.push({ key: monthKey(d), label: monthLabel(d), total: 0, byCategory: emptyCats() })
  }
  const idx = new Map(buckets.map((b, i) => [b.key, i]))
  for (const e of inCurrency(expenses, currency)) {
    const i = idx.get(monthKey(e.occurredOn))
    if (i === undefined) continue
    buckets[i].byCategory[e.category] += e.amount
    buckets[i].total += e.amount
  }
  return buckets
}

export interface CatAgg {
  category: ExpenseCategory
  label: string
  chart: string
  total: number
  count: number
}

export function financeByCategory(expenses: FinanceExpense[], currency: string, mKey?: string): CatAgg[] {
  const rows = inCurrency(expenses, currency).filter((e) => !mKey || monthKey(e.occurredOn) === mKey)
  return CATEGORIES.map((c) => {
    const sub = rows.filter((e) => e.category === c.key)
    return { category: c.key, label: c.label, chart: c.chart, total: sub.reduce((a, e) => a + e.amount, 0), count: sub.length }
  })
    .filter((c) => c.count > 0)
    .sort((a, b) => b.total - a.total)
}

export interface MerchantAgg {
  merchant: string
  total: number
  count: number
}

export function financeByMerchant(expenses: FinanceExpense[], currency: string, mKey?: string): MerchantAgg[] {
  const rows = inCurrency(expenses, currency).filter((e) => !mKey || monthKey(e.occurredOn) === mKey)
  const map = new Map<string, MerchantAgg>()
  for (const e of rows) {
    const cur = map.get(e.merchant) ?? { merchant: e.merchant, total: 0, count: 0 }
    cur.total += e.amount
    cur.count++
    map.set(e.merchant, cur)
  }
  return [...map.values()].sort((a, b) => b.total - a.total)
}

export interface FinanceKpis {
  thisMonth: number
  lastMonth: number
  deltaPct: number | null
  count: number
  avg: number
  topCategory: CatAgg | null
}

export function financeKpis(expenses: FinanceExpense[], currency: string): FinanceKpis {
  const today = new Date()
  const tk = monthKey(new Date(today.getFullYear(), today.getMonth(), 1))
  const lk = monthKey(new Date(today.getFullYear(), today.getMonth() - 1, 1))
  const cur = inCurrency(expenses, currency)
  const thisRows = cur.filter((e) => monthKey(e.occurredOn) === tk)
  const thisMonth = thisRows.reduce((a, e) => a + e.amount, 0)
  const lastMonth = cur.filter((e) => monthKey(e.occurredOn) === lk).reduce((a, e) => a + e.amount, 0)
  return {
    thisMonth,
    lastMonth,
    deltaPct: lastMonth > 0 ? (thisMonth - lastMonth) / lastMonth : null,
    count: thisRows.length,
    avg: thisRows.length ? thisMonth / thisRows.length : 0,
    topCategory: financeByCategory(expenses, currency, tk)[0] ?? null,
  }
}
