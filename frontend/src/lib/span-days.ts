// Dias que ocupa un evento en el calendario (helper puro, sin React).

const MAX_SPAN_DAYS = 62

/** Claves YYYY-MM-DD desde `startsOn` hasta `endsOn` (inclusive); sin `endsOn`, solo el inicio. */
export function spanDays(startsOn: string, endsOn: string | null): string[] {
  if (!endsOn || endsOn <= startsOn) return [startsOn]
  const out: string[] = []
  const [y, m, d] = startsOn.split("-").map(Number)
  const cur = new Date(Date.UTC(y, m - 1, d))
  for (let i = 0; i < MAX_SPAN_DAYS; i++) {
    const key = cur.toISOString().slice(0, 10)
    out.push(key)
    if (key >= endsOn) break
    cur.setUTCDate(cur.getUTCDate() + 1)
  }
  return out
}
