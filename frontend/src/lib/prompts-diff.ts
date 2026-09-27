// Resumen de un cambio de prompt para el diálogo de confirmación. Puro, sin DOM.
//
// No es un diff completo (eso pesa y aquí sobra): cuenta líneas que entran y salen y muestra las
// primeras de cada lado, suficiente para que el dueño confirme sabiendo qué está aplicando.

export interface DiffSummary {
  added: string[]
  removed: string[]
  /** Igual texto (tras normalizar saltos de línea y espacios al final). */
  same: boolean
  charsBefore: number
  charsAfter: number
}

function lines(s: string): string[] {
  return s.replace(/\r\n/g, "\n").split("\n").map((l) => l.replace(/\s+$/, ""))
}

export function diffSummary(before: string, after: string): DiffSummary {
  const a = lines(before)
  const b = lines(after)
  const countA = new Map<string, number>()
  for (const l of a) countA.set(l, (countA.get(l) ?? 0) + 1)
  const countB = new Map<string, number>()
  for (const l of b) countB.set(l, (countB.get(l) ?? 0) + 1)
  const added: string[] = []
  const removed: string[] = []
  for (const l of b) {
    const seenA = countA.get(l) ?? 0
    if (seenA > 0) countA.set(l, seenA - 1)
    else if (l.trim()) added.push(l)
  }
  for (const l of a) {
    const seenB = countB.get(l) ?? 0
    if (seenB > 0) countB.set(l, seenB - 1)
    else if (l.trim()) removed.push(l)
  }
  return {
    added,
    removed,
    same: a.join("\n").trim() === b.join("\n").trim(),
    charsBefore: before.length,
    charsAfter: after.length,
  }
}

export function describeDiff(d: DiffSummary): string {
  if (d.same) return "Sin cambios."
  const parts: string[] = []
  if (d.added.length) parts.push(`${d.added.length} línea${d.added.length === 1 ? "" : "s"} nueva${d.added.length === 1 ? "" : "s"}`)
  if (d.removed.length) parts.push(`${d.removed.length} línea${d.removed.length === 1 ? "" : "s"} quitada${d.removed.length === 1 ? "" : "s"}`)
  if (!parts.length) parts.push("solo cambian espacios o el orden")
  return `${parts.join(", ")} · ${d.charsBefore} → ${d.charsAfter} caracteres`
}
