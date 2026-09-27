// Aplana worker_runs.stats (JSON arbitrario) a chips clave→valor para la UI. Los workers guardan
// formas heterogéneas: planas (ocr/log_purge), 1 nivel (classify.by_tier), 3 niveles
// (summarize/extract → cost.by_source.{id}.{tokens}), arrays (calendar.steps_failed, reprocess.stages)
// y hojas no numéricas (reprocess {error}). Recorremos en profundidad y serializamos cada hoja, para que
// NINGUNA forma pueda romper el render (un objeto como hijo de React tumbaba toda la vista /pipeline).

export interface StatChip {
  k: string
  v: string
}

export function flattenStats(stats: Record<string, unknown>): StatChip[] {
  const out: StatChip[] = []
  walk("", stats, out)
  return out
}

function walk(prefix: string, value: unknown, out: StatChip[]): void {
  if (value === null || value === undefined) {
    out.push({ k: prefix, v: String(value) })
  } else if (Array.isArray(value)) {
    // array de primitivos → un chip "k = a, b, c"; array con objetos → recursión indexada
    if (value.every((x) => x === null || typeof x !== "object")) {
      out.push({ k: prefix, v: value.map(String).join(", ") })
    } else {
      value.forEach((x, i) => walk(`${prefix}[${i}]`, x, out))
    }
  } else if (typeof value === "object") {
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      walk(prefix ? `${prefix}.${k}` : k, v, out)
    }
  } else {
    out.push({ k: prefix, v: String(value) }) // number | string | boolean
  }
}
