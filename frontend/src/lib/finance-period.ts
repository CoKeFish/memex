// Periodo y agregaciones de la vista de finanzas (/finanzas). Funciones PURAS sobre las
// transacciones que trae `@/data` (`fetchFinanceTransactions`): el periodo se elige arriba (un mes
// con flechas, o un rango de 3/6/12 meses o todo) y todo lo demás —resumen, serie mensual, por
// categoría, por contraparte— se recorta a ese periodo en el cliente. Con ~1.600 movimientos el
// cambio de mes es instantáneo y no pega a la API.
//
// Reglas de dinero (las mismas de `lib/finance.ts`, que sigue sirviendo a otras vistas):
// - solo `status === "completada"` cuenta: lo fallido o pendiente se ve, no suma;
// - un `kind === "reembolso"` es plata que VUELVE de un gasto: descuenta del gasto, no es ingreso;
// - nunca se suman monedas distintas (no hay tipo de cambio): todo se filtra por una moneda.

import { CATEGORIES, type CatAgg } from "@/lib/finance"
import type { ExpenseCategory, FinanceTransaction } from "@/types/domain"

// ---- Periodo -------------------------------------------------------------------------------------

export type PeriodMode = "mes" | "3m" | "6m" | "12m" | "todo"

export const PERIOD_MODES: { key: PeriodMode; label: string; hint: string }[] = [
  { key: "mes", label: "Mes", hint: "Un mes; se navega con las flechas" },
  { key: "3m", label: "3 meses", hint: "El mes elegido y los dos anteriores" },
  { key: "6m", label: "6 meses", hint: "El mes elegido y los cinco anteriores" },
  { key: "12m", label: "12 meses", hint: "El mes elegido y los once anteriores" },
  { key: "todo", label: "Todo", hint: "Todo lo que hay" },
]

const SPAN: Record<PeriodMode, number> = { mes: 1, "3m": 3, "6m": 6, "12m": 12, todo: 0 }

export interface Period {
  mode: PeriodMode
  /** Mes ancla `YYYY-MM`: el último del periodo (con `todo` no se usa). */
  month: string
}

const pad = (n: number): string => String(n).padStart(2, "0")

/** `YYYY-MM` de una fecha local. */
export function monthOf(iso: string): string {
  return iso.slice(0, 7)
}

export function monthKeyOfDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`
}

/** Suma (o resta) meses a una clave `YYYY-MM`. */
export function shiftMonth(key: string, delta: number): string {
  const [y, m] = key.split("-").map(Number)
  const d = new Date(y, m - 1 + delta, 1)
  return monthKeyOfDate(d)
}

/** Los meses del periodo, del más viejo al más nuevo. Con `todo`, todos los que tienen datos. */
export function periodMonths(period: Period, available: string[]): string[] {
  if (period.mode === "todo") return [...available].sort()
  const span = SPAN[period.mode]
  const out: string[] = []
  for (let i = span - 1; i >= 0; i--) out.push(shiftMonth(period.month, -i))
  return out
}

/** Meses con al menos un movimiento, del más viejo al más nuevo. */
export function monthsWithData(txns: FinanceTransaction[], currency?: string): string[] {
  const seen = new Set<string>()
  for (const t of txns) {
    if (currency && t.currency !== currency) continue
    seen.add(monthOf(t.occurredOn))
  }
  return [...seen].sort()
}

/** Serie continua de meses entre dos claves (rellena los vacíos: un mes sin movimientos es un 0,
 * no un hueco que corra las barras). */
export function monthRange(from: string, to: string): string[] {
  const out: string[] = []
  let cur = from
  // Tope defensivo: 50 años de meses.
  for (let i = 0; i < 600 && cur <= to; i++) {
    out.push(cur)
    cur = shiftMonth(cur, 1)
  }
  return out
}

/** Las filas del periodo, ya filtradas por moneda. */
export function rowsInPeriod(
  txns: FinanceTransaction[],
  currency: string,
  months: string[],
): FinanceTransaction[] {
  const set = new Set(months)
  return txns.filter((t) => t.currency === currency && set.has(monthOf(t.occurredOn)))
}

// ---- Resumen ---------------------------------------------------------------------------------------

export interface Summary {
  /** Plata que entró (ingresos completados; los reembolsos NO cuentan acá). */
  income: number
  /** Plata que salió (egresos completados menos los reembolsos). */
  expense: number
  /** entró − salió. */
  balance: number
  /** Movimientos que se movieron (completados). */
  count: number
  /** Reembolsos completados (informativo: ya descontados del gasto). */
  refunded: number
  /** Movimientos que NO se movieron (fallidos o pendientes). */
  unsettled: number
}

export function summarize(rows: FinanceTransaction[]): Summary {
  const s: Summary = { income: 0, expense: 0, balance: 0, count: 0, refunded: 0, unsettled: 0 }
  for (const t of rows) {
    if (t.status !== "completada") {
      s.unsettled += 1
      continue
    }
    s.count += 1
    if (t.kind === "reembolso") {
      s.expense -= t.amount
      s.refunded += t.amount
    } else if (t.direction === "egreso") {
      s.expense += t.amount
    } else {
      s.income += t.amount
    }
  }
  s.balance = s.income - s.expense
  return s
}

export interface MonthBar extends Summary {
  key: string
  /** «sep 26» */
  label: string
}

// Nombres propios (no Intl): la abreviatura y el formato cambian con la version de ICU (node dice
// «sept de 26» y el navegador «sep 26»), y la tira de meses tiene que leerse igual en todos lados.
const SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
const LONG = [
  "Enero",
  "Febrero",
  "Marzo",
  "Abril",
  "Mayo",
  "Junio",
  "Julio",
  "Agosto",
  "Septiembre",
  "Octubre",
  "Noviembre",
  "Diciembre",
]

/** «sep 26» */
export function monthShortLabel(key: string): string {
  const [y, m] = key.split("-").map(Number)
  return `${SHORT[m - 1]} ${String(y).slice(2)}`
}

/** «Septiembre 2026» */
export function monthLongLabel(key: string): string {
  const [y, m] = key.split("-").map(Number)
  return `${LONG[m - 1]} ${y}`
}

/** El mes dentro de una frase: «septiembre» si es de este año, «septiembre de 2025» si no.
 * Repetir el año del mes en curso en cada rótulo es ruido. */
export function monthInSentence(key: string, today: Date): string {
  const [y, m] = key.split("-").map(Number)
  const mes = LONG[m - 1].toLowerCase()
  return y === today.getFullYear() ? mes : `${mes} de ${y}`
}

/** Un punto por mes (entró, salió, balance) para la tira de navegación. */
export function monthSeries(
  txns: FinanceTransaction[],
  currency: string,
  months: string[],
): MonthBar[] {
  const byMonth = new Map<string, FinanceTransaction[]>()
  for (const t of txns) {
    if (t.currency !== currency) continue
    const k = monthOf(t.occurredOn)
    const arr = byMonth.get(k)
    if (arr) arr.push(t)
    else byMonth.set(k, [t])
  }
  return months.map((key) => ({
    key,
    label: monthShortLabel(key),
    ...summarize(byMonth.get(key) ?? []),
  }))
}

// ---- Ritmo del mes en curso -------------------------------------------------------------------------

export interface Pace {
  /** Día del mes hasta el que hay datos (hoy, si el mes es el corriente). */
  day: number
  /** Días del mes. */
  days: number
  /** Gasto acumulado hasta ese día en el mes elegido. */
  spent: number
  /** Gasto promedio hasta el MISMO día en los meses previos completos. */
  average: number
  /** Meses previos usados para el promedio. */
  basis: number
}

function daysInMonth(key: string): number {
  const [y, m] = key.split("-").map(Number)
  return new Date(y, m, 0).getDate()
}

function spentThroughDay(
  txns: FinanceTransaction[],
  currency: string,
  month: string,
  day: number,
): number {
  const rows = txns.filter(
    (t) =>
      t.currency === currency &&
      monthOf(t.occurredOn) === month &&
      Number(t.occurredOn.slice(8, 10)) <= day,
  )
  return summarize(rows).expense
}

/** «A esta altura del mes, el promedio era X»: compara el gasto acumulado del mes EN CURSO contra el
 * de los `basis` meses anteriores hasta el mismo día. Sin esto, un mes a mitad de camino siempre
 * parece barato. Devuelve null si el mes elegido no es el corriente. */
export function pace(
  txns: FinanceTransaction[],
  currency: string,
  month: string,
  today: Date,
  basis = 3,
): Pace | null {
  if (month !== monthKeyOfDate(today)) return null
  const day = today.getDate()
  const prev: number[] = []
  for (let i = 1; i <= basis; i++) {
    const k = shiftMonth(month, -i)
    if (daysInMonth(k) < day) continue
    prev.push(spentThroughDay(txns, currency, k, day))
  }
  const usable = prev.filter((v) => v > 0)
  return {
    day,
    days: daysInMonth(month),
    spent: spentThroughDay(txns, currency, month, day),
    average: usable.length ? usable.reduce((a, b) => a + b, 0) / usable.length : 0,
    basis: usable.length,
  }
}

// ---- Cortes ------------------------------------------------------------------------------------------

/** Aporte de una fila al GASTO (un reembolso descuenta); 0 si no se movió o es un ingreso. */
export function expenseOf(t: FinanceTransaction): number {
  if (t.status !== "completada") return 0
  if (t.kind === "reembolso") return -t.amount
  return t.direction === "egreso" ? t.amount : 0
}

/** En qué se fue la plata del periodo: categorías con gasto, de mayor a menor. */
export function byCategory(rows: FinanceTransaction[]): CatAgg[] {
  const totals = new Map<ExpenseCategory, { total: number; count: number }>()
  for (const t of rows) {
    const v = expenseOf(t)
    if (v === 0) continue
    const cur = totals.get(t.category) ?? { total: 0, count: 0 }
    cur.total += v
    cur.count += 1
    totals.set(t.category, cur)
  }
  return CATEGORIES.filter((c) => totals.has(c.key))
    .map((c) => ({
      category: c.key,
      label: c.label,
      chart: c.chart,
      total: totals.get(c.key)!.total,
      count: totals.get(c.key)!.count,
    }))
    .filter((c) => c.total > 0)
    .sort((a, b) => b.total - a.total)
}

export interface MerchantAgg {
  merchant: string
  total: number
  count: number
}

/** A quién se le fue la plata del periodo, de mayor a menor. */
export function byMerchant(rows: FinanceTransaction[]): MerchantAgg[] {
  const map = new Map<string, MerchantAgg>()
  for (const t of rows) {
    const v = expenseOf(t)
    if (v === 0) continue
    const cur = map.get(t.merchant) ?? { merchant: t.merchant, total: 0, count: 0 }
    cur.total += v
    cur.count += 1
    map.set(t.merchant, cur)
  }
  return [...map.values()].filter((m) => m.total > 0).sort((a, b) => b.total - a.total)
}

// ---- Cobertura: ¿el periodo tiene el extracto del banco? --------------------------------------------

/** Meses en los que al menos un movimiento vino de un DOCUMENTO subido (el extracto). Un mes que no
 * está acá solo tiene lo que llegó por correo o notificación: la cifra queda corta. */
export function monthsWithStatement(txns: FinanceTransaction[], currency: string): Set<string> {
  const out = new Set<string>()
  for (const t of txns) {
    if (t.currency === currency && t.fromDocument) out.add(monthOf(t.occurredOn))
  }
  return out
}

export interface Coverage {
  /** Meses del periodo sin extracto (sin contar el mes en curso), del más viejo al más nuevo. */
  missing: string[]
  /** Hay extractos en otros meses: por eso se puede afirmar que a estos les falta. */
  comparable: boolean
  /** El mes en curso: su extracto todavía no existe, el banco lo emite al cerrar el mes. */
  currentMonth: boolean
}

/** Qué meses del periodo no tienen extracto. Solo vale avisar si el resto sí lo tiene: sin ningún
 * extracto cargado, esta cuenta no dice nada. */
export function coverage(months: string[], withStatement: Set<string>, today: Date): Coverage {
  const actual = monthKeyOfDate(today)
  return {
    missing: months.filter((m) => m !== actual && !withStatement.has(m)),
    comparable: withStatement.size > 0,
    currentMonth: months.includes(actual) && !withStatement.has(actual),
  }
}

// ---- Filtros de la lista -------------------------------------------------------------------------------

export interface MovementFilters {
  /** "all" | "ingreso" | "egreso" */
  direction: string
  /** "all" o una categoría. */
  category: string
  /** Texto libre: contraparte, descripción, evidencia, lugar. */
  query: string
  /** Incluir lo que no se movió (fallido/pendiente). */
  includeUnsettled: boolean
}

export const EMPTY_FILTERS: MovementFilters = {
  direction: "all",
  category: "all",
  query: "",
  includeUnsettled: true,
}

export function applyFilters(
  rows: FinanceTransaction[],
  f: MovementFilters,
): FinanceTransaction[] {
  const q = f.query.trim().toLowerCase()
  return rows.filter((t) => {
    if (f.direction !== "all" && t.direction !== f.direction) return false
    if (f.category !== "all" && t.category !== f.category) return false
    if (!f.includeUnsettled && t.status !== "completada") return false
    if (q) {
      const hay = `${t.merchant} ${t.description} ${t.evidence} ${t.placeName ?? ""}`.toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
}
