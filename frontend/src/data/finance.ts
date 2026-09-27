// Superficie de FINANZAS contra la API real (no mocks). Como `email.ts`: funciones async + un
// transform snake_case → camelCase. El dashboard trae las transacciones crudas (ingresos + gastos)
// y agrega en el cliente (las funciones puras de `lib/finance.ts`).

import { apiGet, apiPost } from "@/lib/api"
import type {
  ExpenseCategory,
  FinanceDirection,
  FinanceKind,
  FinanceStatus,
  FinanceTransaction,
} from "@/types/domain"

interface FinanceTransactionApiRow {
  id: number
  direction: string
  status: string
  kind: string
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
  from_document?: boolean
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
    status: r.status as FinanceStatus,
    kind: r.kind as FinanceKind,
    amount: r.amount,
    currency: r.currency,
    // El "comercio" es la contraparte (quién cobró/pagó); si vino vacía caemos a `place` (lugar/URL)
    // y si no, a un guion para no mostrar un nombre vacío en la tabla / top comercios.
    merchant: r.counterparty || r.place || "—",
    counterparty: r.counterparty ?? "",
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
    fromDocument: r.from_document ?? false,
  }
}

export interface FetchFinanceOpts {
  currency?: string
  /** Filtra por sentido (ingreso|egreso); por defecto trae AMBOS. */
  direction?: FinanceDirection
  /** Estado: la API por defecto devuelve solo 'completada'; el dashboard pide 'all' y muestra
   *  fallidas/pendientes con su estado (sin contarlas). */
  status?: FinanceStatus | "all"
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
 * todo (hasta `max`), sin filtro de dirección y con TODOS los estados (`status=all`): lo que no se
 * movió (fallida/pendiente) se ve en la tabla con su estado pero no entra en KPIs ni gráficos.
 */
export interface PendingPayment {
  id: number
  status: string
  kind: string
  amount: number
  currency: string
  category: string
  counterparty: string
  occurredAt: string
  description: string
  ageDays: number
}

/** Pagos fallidos o pendientes sin un pago exitoso posterior al mismo servicio (GET /finance/pending). */
/** «Este comercio es <rubro>»: guarda la regla por contraparte y la aplica a todo lo suyo. */
export async function setFinanceRubro(
  counterparty: string,
  category: ExpenseCategory,
): Promise<{ applied: number }> {
  const r = await apiPost<{ applied: number }>("/finance/rubros", { counterparty, category })
  return { applied: r.applied }
}

export async function fetchPendingPayments(days = 30): Promise<PendingPayment[]> {
  const r = await apiGet<{
    items: {
      id: number
      status: string
      kind: string
      amount: number
      currency: string
      category: string
      counterparty: string
      occurred_at: string
      description: string
      age_days: number
    }[]
  }>(`/finance/pending?days=${days}`)
  return r.items.map((i) => ({
    id: i.id,
    status: i.status,
    kind: i.kind,
    amount: i.amount,
    currency: i.currency,
    category: i.category,
    counterparty: i.counterparty,
    occurredAt: i.occurred_at,
    description: i.description,
    ageDays: i.age_days,
  }))
}

export interface Subscription {
  id: number
  provider: string
  currency: string
  /** Monto habitual; null si todavía no se vio ningún cobro. */
  amountTypical: number | null
  amountVaries: boolean
  /** «cada mes, el día 3» */
  cadenceHuman: string
  nextRenewalOn: string | null
  /** Días hasta la próxima renovación; negativo = ya pasó sin verse el cobro. */
  daysUntil: number | null
  status: string
  charges: number
  confidence: number
}

export interface SubscriptionCommitment {
  currency: string
  /** Costo mensual comprometido en esa moneda (lo anual /12, lo semanal x4,33). */
  monthly: number
  subscriptions: number
}

interface SubscriptionApi {
  id: number
  provider: string
  currency: string
  amount_typical: number | null
  amount_varies: boolean
  cadence_human: string
  next_renewal_on: string | null
  days_until: number | null
  status: string
  charges: number
  confidence: number
}

/** Los cobros que se repiten (GET /finance/subscriptions), lo más próximo a renovar primero. */
export async function fetchSubscriptions(): Promise<{
  items: Subscription[]
  commitment: SubscriptionCommitment[]
}> {
  const r = await apiGet<{
    items: SubscriptionApi[]
    monthly_commitment: SubscriptionCommitment[]
  }>("/finance/subscriptions")
  return {
    items: r.items.map((s) => ({
      id: s.id,
      provider: s.provider,
      currency: s.currency,
      amountTypical: s.amount_typical,
      amountVaries: s.amount_varies,
      cadenceHuman: s.cadence_human,
      nextRenewalOn: s.next_renewal_on,
      daysUntil: s.days_until,
      status: s.status,
      charges: s.charges,
      confidence: s.confidence,
    })),
    commitment: r.monthly_commitment ?? [],
  }
}

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
    qs.set("status", opts?.status ?? "all")
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
