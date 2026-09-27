// Filtro de la vista /calendario por estado (helper puro, sin React).
import type { ConsolidatedEvent } from "@/types/domain"

export type CalendarFilter = "todos" | "mios" | "confirmado" | "provisional" | "anuncio"

export const CALENDAR_FILTERS: { key: CalendarFilter; label: string; hint: string }[] = [
  { key: "todos", label: "Todo", hint: "todos los eventos" },
  { key: "mios", label: "Mis eventos", hint: "confirmados + provisionales" },
  { key: "confirmado", label: "Confirmados", hint: "estás inscrito, pagaste o confirmaste" },
  { key: "provisional", label: "Provisionales", hint: "te hablan como participante, sin confirmación" },
  { key: "anuncio", label: "Anuncios", hint: "convocatorias y eventos abiertos; no son compromisos tuyos" },
]

export function applyCalendarFilter(events: ConsolidatedEvent[], filter: CalendarFilter): ConsolidatedEvent[] {
  if (filter === "todos") return events
  if (filter === "mios") return events.filter((e) => e.commitment === "confirmado" || e.commitment === "provisional")
  return events.filter((e) => (e.commitment ?? "anuncio") === filter)
}

/**
 * Filtro con el que abre /calendario. Decisión del dueño (2026-09-26): una propuesta de grupo
 * («les propongo que hagamos X el 26, ¿quién se apunta?») se captura como anuncio pero NO es su
 * calendario hasta que sea un plan concreto al que va. Por eso el default es «Mis eventos»
 * (confirmados + provisionales) y los anuncios se ven solo al pedirlos. La elección se recuerda
 * por navegador (localStorage); sin storage, o con un valor raro, vuelve al default.
 */
export const DEFAULT_CALENDAR_FILTER: CalendarFilter = "mios"
export const CALENDAR_FILTER_STORAGE_KEY = "memex.calendar.filter"

interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

const VALID = new Set<string>(CALENDAR_FILTERS.map((f) => f.key))

export function rememberedCalendarFilter(storage: StorageLike | null | undefined): CalendarFilter {
  try {
    const raw = storage?.getItem(CALENDAR_FILTER_STORAGE_KEY)
    return raw && VALID.has(raw) ? (raw as CalendarFilter) : DEFAULT_CALENDAR_FILTER
  } catch {
    return DEFAULT_CALENDAR_FILTER
  }
}

export function rememberCalendarFilter(storage: StorageLike | null | undefined, filter: CalendarFilter): void {
  try {
    storage?.setItem(CALENDAR_FILTER_STORAGE_KEY, filter)
  } catch {
    // sin storage (privado, bloqueado): la elección vive solo en esta sesión
  }
}
