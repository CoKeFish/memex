// Zona horaria de display de /metricas + construcción de ventanas temporales alineadas a esa TZ.
//
// El backend bucketiza el día por `tz` (param de /metrics/llm/rollup); el cliente computa la
// medianoche-en-TZ como instante UTC para que "hoy"/los días del eje coincidan con el reloj de pared
// del usuario (no con la TZ del navegador ni con una TZ hardcodeada). Solo `Intl` (no hay
// date-fns-tz). La TZ activa = override (localStorage) ?? autodetectada. Zonas sin DST (Bogota,
// Mexico_City) → exacto; en zonas con DST el cálculo puede errar a lo sumo el salto, solo en el
// instante de transición cerca de medianoche (aceptable para una vista de costo).

const TZ_KEY = "memex.metricsTz"

/** TZ autodetectada del navegador (p. ej. "America/Bogota"). */
export function autodetectedTz(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

/** TZ activa de display: override guardado, o la autodetectada. */
export function activeDisplayTz(): string {
  return localStorage.getItem(TZ_KEY) ?? autodetectedTz()
}

/** Persiste (o limpia) el override de TZ. `null` = volver a autodetectar. */
export function setDisplayTzOverride(tz: string | null): void {
  if (tz === null) localStorage.removeItem(TZ_KEY)
  else localStorage.setItem(TZ_KEY, tz)
}

/** Offset (minutos) de `tz` respecto a UTC en el instante `at`, derivado vía `Intl`. */
function tzOffsetMinutes(at: Date, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(at)
  const f: Record<string, string> = {}
  for (const p of parts) f[p.type] = p.value
  const hour = f.hour === "24" ? "00" : f.hour // algunos engines devuelven "24" a medianoche
  const asUtc = Date.UTC(+f.year, +f.month - 1, +f.day, +hour, +f.minute, +f.second)
  return (asUtc - at.getTime()) / 60000
}

/** Instante UTC (ISO) de la medianoche del día (`y`, `m`=1..12, `d`) en `tz`. */
export function startOfDayInTz(y: number, m: number, d: number, tz: string): string {
  const guess = Date.UTC(y, m - 1, d, 0, 0, 0)
  const offset = tzOffsetMinutes(new Date(guess), tz)
  return new Date(guess - offset * 60_000).toISOString()
}

/** `{y, m, d}` del día de pared actual en `tz`. */
export function todayInTz(tz: string): { y: number; m: number; d: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date())
  const f: Record<string, string> = {}
  for (const p of parts) f[p.type] = p.value
  return { y: +f.year, m: +f.month, d: +f.day }
}
