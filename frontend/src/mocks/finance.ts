import { Rng } from "@/lib/rng"
import type { ExpenseCategory, FinanceExpense } from "@/types/domain"
import { inbox, NOW } from "./index"

const DAY = 86_400_000

interface MerchantDef {
  name: string
  category: ExpenseCategory
  currency: string
  min: number
  max: number
  weight: number
}

// Catálogo de comercios → categoría + moneda + rango típico. Mayoría MXN (es-MX),
// suscripciones en USD, y un comercio en ARS (calca el smoke real: $160.000 ARS).
const MERCHANTS: MerchantDef[] = [
  { name: "Rappi", category: "comida", currency: "MXN", min: 85, max: 420, weight: 14 },
  { name: "OXXO", category: "comida", currency: "MXN", min: 18, max: 160, weight: 12 },
  { name: "Café Cardinal", category: "comida", currency: "MXN", min: 45, max: 130, weight: 10 },
  { name: "La Comer", category: "comida", currency: "MXN", min: 210, max: 1600, weight: 7 },
  { name: "Uber", category: "transporte", currency: "MXN", min: 38, max: 210, weight: 11 },
  { name: "DiDi", category: "transporte", currency: "MXN", min: 32, max: 180, weight: 8 },
  { name: "Spotify", category: "entretenimiento", currency: "MXN", min: 115, max: 169, weight: 4 },
  { name: "Netflix", category: "entretenimiento", currency: "MXN", min: 219, max: 299, weight: 3 },
  { name: "Railway", category: "software", currency: "USD", min: 1, max: 12, weight: 5 },
  { name: "Vercel", category: "software", currency: "USD", min: 0, max: 20, weight: 4 },
  { name: "OpenAI", category: "software", currency: "USD", min: 5, max: 28, weight: 5 },
  { name: "Coursera", category: "educacion", currency: "USD", min: 39, max: 59, weight: 2 },
  { name: "UAEM (matrícula)", category: "educacion", currency: "MXN", min: 1500, max: 4200, weight: 2 },
  { name: "Farmacia del Ahorro", category: "salud", currency: "MXN", min: 55, max: 680, weight: 5 },
  { name: "CFE", category: "servicios", currency: "MXN", min: 210, max: 920, weight: 3 },
  { name: "Telmex", category: "servicios", currency: "MXN", min: 389, max: 629, weight: 3 },
  { name: "Comercio XYZ", category: "otros", currency: "ARS", min: 50_000, max: 200_000, weight: 3 },
]

const rng = new Rng(31415926)

function isoDate(msAgo: number): string {
  return new Date(NOW.getTime() - msAgo).toISOString().slice(0, 10)
}

export const financeExpenses: FinanceExpense[] = Array.from({ length: 150 }, (_, idx) => {
  const id = idx + 1
  const m = rng.weighted(MERCHANTS, MERCHANTS.map((x) => x.weight))
  const raw = rng.float(m.min, m.max)
  const amount = m.currency === "ARS" ? Math.round(raw / 100) * 100 : Math.round(raw * 100) / 100
  const occurredMsAgo = rng.float(0, 120 * DAY)
  const occurredOn = isoDate(occurredMsAgo)
  const inboxId = rng.int(1, inbox.length)
  const amountStr =
    m.currency === "ARS" ? `$${amount.toLocaleString("es-AR")}` : m.currency === "USD" ? `US$${amount.toFixed(2)}` : `$${amount.toFixed(2)}`
  return {
    id,
    amount,
    currency: m.currency,
    merchant: m.name,
    category: m.category,
    occurredOn,
    description: `${m.name} — ${m.category}`,
    evidence: rng.bool(0.5) ? `Cargo aprobado: ${amountStr} en ${m.name}` : `Total facturado: ${amountStr}`,
    sourceInboxIds: [inboxId],
    createdAt: isoDate(Math.max(0, occurredMsAgo - rng.int(1, 3) * DAY)),
  }
})
