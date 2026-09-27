// Estado de retorno del feed /datos: al abrir un mensaje y volver, la lista restaura el MISMO
// filtro (vive en la URL) y el MISMO progreso de scroll. Los filtros viajan en los search params;
// acá solo persiste el ancla de scroll + el search con el que se guardó (la restauración aplica
// únicamente si el search coincide — volver con otro filtro no debe saltar a un offset ajeno).
// sessionStorage a propósito: por-pestaña y muere con la sesión del navegador.

export interface FeedReturnState {
  /** `params.toString()` del feed al desmontar (sin "?"); "" = sin filtros. */
  search: string
  /** Key estable del primer item visible (`r:{id}` / `h:{label}`); null si no se pudo derivar. */
  anchorKey: string | null
  /** Píxeles de scroll DENTRO del item ancla (scrollTop - start del item). */
  anchorDelta: number
  /** scrollTop absoluto — fallback si el ancla ya no existe (data cambió entre visitas). */
  scrollTop: number
  /** Páginas (de lo más nuevo hacia atrás) que había cargadas al salir: al volver se piden las
   * mismas ANTES de restaurar, así el ancla existe aunque estuviera en la página 3. */
  pages: number
}

const KEY = "memex.feed.return"

/** try/catch defensivo: sessionStorage puede no existir (tests node) o lanzar (modo privado). */
export function saveFeedReturn(state: FeedReturnState): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(state))
  } catch {
    // sin storage no hay restauración — degrada en silencio
  }
}

export function loadFeedReturn(): FeedReturnState | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as Partial<FeedReturnState> | null
    if (!v || typeof v !== "object" || typeof v.search !== "string") return null
    return {
      search: v.search,
      anchorKey: typeof v.anchorKey === "string" ? v.anchorKey : null,
      anchorDelta: typeof v.anchorDelta === "number" ? v.anchorDelta : 0,
      scrollTop: typeof v.scrollTop === "number" ? v.scrollTop : 0,
      pages: typeof v.pages === "number" && v.pages >= 1 ? Math.floor(v.pages) : 1,
    }
  } catch {
    return null
  }
}

/** Fuente activa del feed según el `search` guardado ("source=3&q=x" → 3); undefined = todas.
 * La navegación anterior/siguiente del detalle la usa para moverse dentro del mismo filtro. */
export function feedSourceIdFromSearch(search: string | null | undefined): number | undefined {
  if (!search) return undefined
  const raw = new URLSearchParams(search).get("source")
  if (!raw || raw === "all") return undefined
  const n = Number(raw)
  return Number.isInteger(n) && n > 0 ? n : undefined
}

/** Al saltar a otro mensaje desde el detalle, el ancla de retorno pasa a ser ESE mensaje: volver a
 * la lista aterriza donde terminaste, no donde empezaste. Sin estado guardado no crea uno (el feed
 * decide el filtro al volver). */
export function retargetFeedReturn(inboxId: number): void {
  const st = loadFeedReturn()
  if (!st) return
  saveFeedReturn({ ...st, anchorKey: `r:${inboxId}`, anchorDelta: 0 })
}

/** load + remove: la restauración es one-shot (una visita fresca no debe re-saltar). */
export function consumeFeedReturn(): FeedReturnState | null {
  const state = loadFeedReturn()
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    // ya devolvimos el estado; no poder borrar solo repite la restauración la próxima vez
  }
  return state
}
