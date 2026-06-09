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
    }
  } catch {
    return null
  }
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
