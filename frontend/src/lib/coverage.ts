// Geometría pura del timeline de cobertura: rangos de días sobre un dominio temporal.
// Sin React/DOM (testeable en vitest, ambiente node). Los días viajan como "YYYY-MM-DD" y TODA la
// aritmética/formateo va en UTC explícito para no desplazar fechas por la TZ local (cf. la nota de
// `formatDateOnly` en format.ts: `new Date("YYYY-MM-DD")` es medianoche UTC; en UTC-5 retrocede un
// día si se formatea en local).

export interface DayRange {
  start: string // primer día del tramo (inclusive)
  end: string // último día del tramo (inclusive)
  count: number // items dentro del tramo
}

export interface DayDomain {
  min: string
  max: string
}

const MS_PER_DAY = 86_400_000

function dayMs(day: string): number {
  const [y, m, d] = day.split("-").map(Number)
  return Date.UTC(y, m - 1, d)
}

function dayIndex(day: string, domain: DayDomain): number {
  return Math.round((dayMs(day) - dayMs(domain.min)) / MS_PER_DAY)
}

function spanDays(start: string, end: string): number {
  return Math.round((dayMs(end) - dayMs(start)) / MS_PER_DAY) + 1
}

/** Días de calendario del dominio, inclusive en ambos extremos (min == max → 1). */
export function domainDays(domain: DayDomain): number {
  return spanDays(domain.min, domain.max)
}

/** Posición de un rango como % del dominio: cada día es una celda de igual ancho
 *  (left = idx(start)/N; width = días del rango/N). */
export function segmentPosition(
  r: { start: string; end: string },
  domain: DayDomain,
): { leftPct: number; widthPct: number } {
  const n = domainDays(domain)
  return {
    leftPct: (dayIndex(r.start, domain) / n) * 100,
    widthPct: (spanDays(r.start, r.end) / n) * 100,
  }
}

export interface VisualSegment extends DayRange {
  days: number
  /** Cuántos rangos reales quedaron fundidos en este segmento visual (1 = ninguno). */
  merged: number
}

/** Funde rangos ORDENADOS cuyo hueco proyectado en px sea menor a `minGapPx`. Acota los nodos DOM
 *  al ancho disponible (con dominios de años, cientos de tramos sueltos caben en menos píxeles que
 *  segmentos) y de paso elimina el ruido visual sub-pixel. */
export function mergeForWidth(
  ranges: DayRange[],
  domain: DayDomain,
  widthPx: number,
  minGapPx = 1,
): VisualSegment[] {
  const pxPerDay = widthPx / domainDays(domain)
  const out: VisualSegment[] = []
  for (const r of ranges) {
    const prev = out[out.length - 1]
    if (prev) {
      const gapDays = dayIndex(r.start, domain) - dayIndex(prev.end, domain) - 1
      if (gapDays * pxPerDay < minGapPx) {
        prev.end = r.end
        prev.count += r.count
        prev.days = spanDays(prev.start, prev.end)
        prev.merged += 1
        continue
      }
    }
    out.push({ ...r, days: spanDays(r.start, r.end), merged: 1 })
  }
  return out
}

export interface AxisTick {
  day: string // "YYYY-MM-DD" del tick
  label: string
  pct: number // posición sobre el dominio (0..100)
}

// Formatters del eje en UTC explícito (ver nota del módulo).
const tickMonthFmt = new Intl.DateTimeFormat("es-MX", {
  month: "short",
  year: "2-digit",
  timeZone: "UTC",
})
const tickYearFmt = new Intl.DateTimeFormat("es-MX", { year: "numeric", timeZone: "UTC" })
const tickDayFmt = new Intl.DateTimeFormat("es-MX", {
  day: "2-digit",
  month: "short",
  timeZone: "UTC",
})

function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

/** Ticks adaptativos del eje: primeros de mes ("ene 26"); si exceden `maxTicks` degrada a
 *  trimestres, luego a eneros ("2026"), luego a eneros cada k años. Dominios tan cortos que no
 *  contienen ningún 1ro de mes caen a los extremos del dominio ("05 jun"). */
export function axisTicks(domain: DayDomain, maxTicks = 12): AxisTick[] {
  const months: { day: string; month: number; year: number }[] = []
  const first = new Date(dayMs(domain.min))
  let y = first.getUTCFullYear()
  let m = first.getUTCMonth()
  if (first.getUTCDate() > 1) {
    m += 1 // el 1ro de este mes quedó fuera del dominio; arrancar en el siguiente
  }
  const maxMs = dayMs(domain.max)
  for (;;) {
    y += Math.floor(m / 12)
    m = ((m % 12) + 12) % 12
    const ms = Date.UTC(y, m, 1)
    if (ms > maxMs) break
    months.push({ day: isoDay(ms), month: m, year: y })
    m += 1
  }

  if (months.length === 0) {
    const ends = domain.min === domain.max ? [domain.min] : [domain.min, domain.max]
    return ends.map((day) => ({
      day,
      label: tickDayFmt.format(new Date(dayMs(day))),
      pct: (dayIndex(day, domain) / domainDays(domain)) * 100,
    }))
  }

  let picked = months
  if (picked.length > maxTicks) picked = months.filter((t) => t.month % 3 === 0)
  if (picked.length > maxTicks) picked = months.filter((t) => t.month === 0)
  if (picked.length > maxTicks) {
    const k = Math.ceil(picked.length / maxTicks)
    const firstYear = picked[0].year
    picked = picked.filter((t) => (t.year - firstYear) % k === 0)
  }
  const yearly = picked.every((t) => t.month === 0) && months.some((t) => t.month !== 0)

  const n = domainDays(domain)
  return picked.map((t) => ({
    day: t.day,
    label: yearly
      ? tickYearFmt.format(new Date(dayMs(t.day)))
      : tickMonthFmt.format(new Date(dayMs(t.day))),
    pct: (dayIndex(t.day, domain) / n) * 100,
  }))
}
