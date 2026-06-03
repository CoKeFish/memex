// Helpers de ventana para los paneles del calendario (puros, testeables). Evitan listas larguísimas:
// por defecto se muestra un recorte relevante (próximos / este mes / últimos N + próximos M) y el
// usuario expande a "Todos". La referencia temporal es `NOW` (igual que month-grid/Agenda).

import { NOW } from "@/data"

/** Hoy como `YYYY-MM-DD` (local). */
export function todayKey(): string {
  return `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, "0")}-${String(NOW.getDate()).padStart(2, "0")}`
}

/** Mes actual como `YYYY-MM`. */
export function monthKey(): string {
  return `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, "0")}`
}

/** Items cuya fecha (accessor) es de hoy en adelante (próximos / vigentes). */
export function upcoming<T>(items: T[], dateOf: (t: T) => string): T[] {
  const t = todayKey()
  return items.filter((it) => dateOf(it) >= t)
}

/** Items cuya fecha cae en el mes actual. */
export function thisMonth<T>(items: T[], dateOf: (t: T) => string): T[] {
  const m = monthKey()
  return items.filter((it) => dateOf(it).startsWith(m))
}

/** Ventana compacta alrededor de hoy: los `past` más recientes ya pasados + los `future` próximos
 *  (ordenados por fecha ascendente). Ej. dedup: últimos 5 + próximos 4. */
export function recentWindow<T>(
  items: T[],
  dateOf: (t: T) => string,
  past: number,
  future: number,
): T[] {
  const t = todayKey()
  const sorted = [...items].sort((a, b) => {
    const da = dateOf(a)
    const db = dateOf(b)
    return da < db ? -1 : da > db ? 1 : 0
  })
  const pastItems = sorted.filter((it) => dateOf(it) < t).slice(-past)
  const futureItems = sorted.filter((it) => dateOf(it) >= t).slice(0, future)
  return [...pastItems, ...futureItems]
}
