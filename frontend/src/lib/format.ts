// Formateadores localizados (es-MX). Números tabulares para tablas de métricas.

const intFmt = new Intl.NumberFormat("es-MX")
const usdFmt = new Intl.NumberFormat("es-MX", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const usdFineFmt = new Intl.NumberFormat("es-MX", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
})

export function formatInt(n: number): string {
  return intFmt.format(Math.round(n))
}

/** Costo en USD: 2 decimales por defecto; bajo $0.01 muestra hasta 4 para no aplastar a 0. */
export function formatUsd(n: number): string {
  if (n !== 0 && Math.abs(n) < 0.01) {
    return new Intl.NumberFormat("es-MX", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    }).format(n)
  }
  return usdFmt.format(n)
}

/** Costo con precisión fina (tooltips): hasta 6 decimales. */
export function formatUsdFine(n: number): string {
  return usdFineFmt.format(n)
}

export function formatPct(n: number, digits = 1): string {
  return `${(n * 100).toFixed(digits)}%`
}

/** Monto en cualquier moneda (USD/MXN/ARS…). */
export function formatMoney(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat("es-MX", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${amount.toFixed(2)} ${currency}`
  }
}

const monthFmt = new Intl.DateTimeFormat("es-MX", { month: "short", year: "2-digit" })
const monthLongFmt = new Intl.DateTimeFormat("es-MX", { month: "long", year: "numeric" })

/** "ene 26" a partir de "2026-01-..". */
export function monthLabel(d: Date | string): string {
  return monthFmt.format(typeof d === "string" ? new Date(d) : d)
}
export function monthLongLabel(d: Date | string): string {
  return monthLongFmt.format(typeof d === "string" ? new Date(d) : d)
}
/** Clave de mes "2026-01". Timezone-safe: una string `YYYY-MM[-DD]` se corta tal cual (NO se
 *  reinterpreta como UTC), y un `Date` usa sus componentes LOCALES — evita que `.toISOString()`
 *  desplace el mes en zonas no-UTC (p. ej. el inicio de mes local caía en el mes anterior en UTC). */
export function monthKey(d: Date | string): string {
  if (typeof d === "string") return d.slice(0, 7)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
}

/** Conteos grandes de tokens: 1.2k / 3.4M. */
export function formatCompact(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(Math.round(n))
}

export function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m ${rem}s`
}

const dateTimeFmt = new Intl.DateTimeFormat("es-MX", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
})
const dateFmt = new Intl.DateTimeFormat("es-MX", {
  day: "2-digit",
  month: "short",
  year: "numeric",
})

export function formatDateTime(d: Date | string): string {
  return dateTimeFmt.format(typeof d === "string" ? new Date(d) : d)
}

export function formatDate(d: Date | string): string {
  return dateFmt.format(typeof d === "string" ? new Date(d) : d)
}

// Fecha-sólo `YYYY-MM-DD` (sin hora): se interpreta y formatea en UTC para NO desplazarla por la
// zona horaria local (`new Date("2026-05-31")` es UTC-medianoche; formatear en local mostraría el
// día anterior en zonas negativas como UTC-5). Para campos DATE como `occurred_on`.
const dateOnlyFmt = new Intl.DateTimeFormat("es-MX", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
})

/** Formatea una fecha-sólo `YYYY-MM-DD` sin off-by-one por timezone (cf. `formatDate`). */
export function formatDateOnly(d: string): string {
  return dateOnlyFmt.format(new Date(d))
}

/** "hace 3 min" / "hace 2 h" / "hace 4 d" / "en 2 h". */
export function formatRelative(d: Date | string, now: Date = new Date()): string {
  const date = typeof d === "string" ? new Date(d) : d
  const diffMs = date.getTime() - now.getTime()
  const past = diffMs <= 0
  const abs = Math.abs(diffMs)
  const sec = Math.round(abs / 1000)
  const min = Math.round(sec / 60)
  const hr = Math.round(min / 60)
  const day = Math.round(hr / 24)
  let body: string
  if (sec < 45) body = `${sec}s`
  else if (min < 60) body = `${min} min`
  else if (hr < 24) body = `${hr} h`
  else if (day < 30) body = `${day} d`
  else body = formatDate(date)
  if (day >= 30) return body
  return past ? `hace ${body}` : `en ${body}`
}

export type AgeBucket = "fresh" | "warn" | "stale"

/** Semáforo de frescura: <6h fresco, <24h aviso, ≥24h estancado. */
export function ageBucket(d: Date | string, now: Date = new Date()): AgeBucket {
  const date = typeof d === "string" ? new Date(d) : d
  const hours = (now.getTime() - date.getTime()) / 3_600_000
  if (hours < 6) return "fresh"
  if (hours < 24) return "warn"
  return "stale"
}
