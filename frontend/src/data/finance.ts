// Superficie de FINANZAS contra la API real (no mocks). Como `email.ts`: funciones async + un
// transform snake_case → camelCase. El dashboard trae las transacciones crudas (ingresos + gastos)
// y agrega en el cliente (las funciones puras de `lib/finance.ts`).

import { apiGet } from "@/lib/api"
import type { ExpenseCategory, FinanceDirection, FinanceTransaction } from "@/types/domain"

interface FinanceTransactionApiRow {
  id: number
  direction: string
  amount: number
  currency: string
  category: string
  counterparty: string
  place: string
  occurred_at: string
  occurred_at_precision: string
  description: string
  evidence: string
  source_inbox_ids: number[]
  created_at: string
  place_name: string | null
  place_address: string | null
}

interface FinanceTransactionApiList {
  items: FinanceTransactionApiRow[]
  next_cursor: number | null
}

/** Fecha calendario LOCAL de un timestamp ISO, como `YYYY-MM-DD` (sin desplazamiento UTC). */
function localDateKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

function toFinanceTransaction(r: FinanceTransactionApiRow): FinanceTransaction {
  return {
    id: r.id,
    direction: r.direction as FinanceDirection,
    amount: r.amount,
    currency: r.currency,
    // El "comercio" es la contraparte (quién cobró/pagó); si vino vacía caemos a `place` (lugar/URL)
    // y si no, a un guion para no mostrar un nombre vacío en la tabla / top comercios.
    merchant: r.counterparty || r.place || "—",
    category: r.category as ExpenseCategory,
    // `occurred_at` SIEMPRE viene (TIMESTAMPTZ NOT NULL: el instante del cobro, o inferido de la
    // recepción del mensaje). Lo bajamos a la fecha calendario LOCAL para el bucketeo por mes
    // (`monthKey`); usamos la fecha local, no `slice(0,10)` sobre el ISO UTC, así no se corre un día
    // —y por ende un mes— en zonas negativas como UTC-5.
    occurredOn: localDateKey(r.occurred_at),
    description: r.description,
    evidence: r.evidence,
    sourceInboxIds: r.source_inbox_ids,
    createdAt: r.created_at,
    placeName: r.place_name ?? null,
    placeAddress: r.place_address ?? null,
  }
}

export interface FetchFinanceOpts {
  currency?: string
  /** Filtra por sentido (ingreso|egreso); por defecto trae AMBOS. */
  direction?: FinanceDirection
  /** occurred_at >= since (YYYY-MM-DD) */
  since?: string
  /** occurred_at < until (YYYY-MM-DD) */
  until?: string
  /** Tope total de filas a traer (paginando). */
  max?: number
}

/**
 * Todas las transacciones del usuario (GET /finance/transactions: ingresos + gastos), paginando por
 * cursor igual que `fetchInbox`. El dashboard filtra/agrega en el cliente, así que por defecto trae
 * todo (hasta `max`) y sin filtro de dirección.
 */
export async function fetchFinanceTransactions(
  opts?: FetchFinanceOpts,
): Promise<FinanceTransaction[]> {
  const max = opts?.max ?? 5000
  const pageSize = 500
  const out: FinanceTransaction[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams()
    if (opts?.currency) qs.set("currency", opts.currency)
    if (opts?.direction) qs.set("direction", opts.direction)
    if (opts?.since) qs.set("since", opts.since)
    if (opts?.until) qs.set("until", opts.until)
    qs.set("limit", String(pageSize))
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<FinanceTransactionApiList>(`/finance/transactions?${qs.toString()}`)
    out.push(...page.items.map(toFinanceTransaction))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}
