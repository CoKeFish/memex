// Superficie de FINANZAS contra la API real (no mocks). Como `email.ts`: funciones async + un
// transform snake_case → camelCase. El dashboard trae los gastos crudos y agrega en el cliente
// (las funciones puras de `lib/finance.ts`).

import { apiGet } from "@/lib/api"
import type { ExpenseCategory, FinanceExpense } from "@/types/domain"

interface FinanceExpenseApiRow {
  id: number
  amount: number
  currency: string
  category: string
  merchant: string
  occurred_on: string | null
  description: string
  evidence: string
  source_inbox_ids: number[]
  created_at: string
}

interface FinanceExpenseApiList {
  items: FinanceExpenseApiRow[]
  next_cursor: number | null
}

function toFinanceExpense(r: FinanceExpenseApiRow): FinanceExpense {
  return {
    id: r.id,
    amount: r.amount,
    currency: r.currency,
    merchant: r.merchant,
    category: r.category as ExpenseCategory,
    // `occurred_on` puede ser null (el LLM no logró fechar el cargo): caemos a la fecha de creación
    // para que el bucketeo por mes (`monthKey`) siempre tenga una fecha válida.
    occurredOn: r.occurred_on ?? r.created_at.slice(0, 10),
    description: r.description,
    evidence: r.evidence,
    sourceInboxIds: r.source_inbox_ids,
    createdAt: r.created_at,
  }
}

export interface FetchFinanceOpts {
  currency?: string
  /** occurred_on >= since (YYYY-MM-DD) */
  since?: string
  /** occurred_on < until (YYYY-MM-DD) */
  until?: string
  /** Tope total de filas a traer (paginando). */
  max?: number
}

/**
 * Todos los gastos del usuario (GET /finance/expenses), paginando por cursor igual que `fetchInbox`.
 * El dashboard filtra/agrega en el cliente, así que por defecto trae todo (hasta `max`).
 */
export async function fetchFinanceExpenses(opts?: FetchFinanceOpts): Promise<FinanceExpense[]> {
  const max = opts?.max ?? 5000
  const pageSize = 500
  const out: FinanceExpense[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams()
    if (opts?.currency) qs.set("currency", opts.currency)
    if (opts?.since) qs.set("since", opts.since)
    if (opts?.until) qs.set("until", opts.until)
    qs.set("limit", String(pageSize))
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<FinanceExpenseApiList>(`/finance/expenses?${qs.toString()}`)
    out.push(...page.items.map(toFinanceExpense))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}
