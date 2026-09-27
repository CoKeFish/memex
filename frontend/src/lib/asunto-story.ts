// Helpers puros de la ventana de un asunto (/asuntos/:id): la agenda «qué sigue» a partir de las
// tareas, la forma de un dato (enlace, código o texto), la cronología por mes y los cortes de la
// evidencia (archivos) y del elenco (personas y organizaciones vs. el resto). Sin React: se prueban
// solos en node.

import type { AsuntoElencoItem, AsuntoEvidence, AsuntoHito } from "@/data/asuntos"
import { groupTareas, type Tarea } from "@/data/tareas"
import { monthKey, monthLongLabel } from "@/lib/format"

// ---- Fechas --------------------------------------------------------------------------------------

function pad(n: number): string {
  return String(n).padStart(2, "0")
}

/** `YYYY-MM-DD` de HOY en la zona horaria del navegador (no UTC: a las 20:00 de Bogotá ya es
 * mañana en UTC y la agenda marcaría «venció» una tarea de hoy). */
export function localIsoDate(d: Date = new Date()): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// Nombres propios (no Intl): la abreviatura de septiembre cambia según la versión de ICU («sep» en
// el navegador, «sept» en node) y la agenda debe leerse igual en todos lados.
const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
const WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]

export interface DateParts {
  day: number
  /** «sep» (sin punto, minúscula). */
  month: string
  /** «miércoles». */
  weekday: string
  year: number
}

/** Partes de una fecha-sólo para pintarla como tablero (día grande, mes chico). Se interpreta a
 * mediodía local para no correrse de día por la zona horaria. */
export function dateParts(iso: string): DateParts {
  const d = new Date(`${iso}T12:00:00`)
  return {
    day: d.getDate(),
    month: MONTHS[d.getMonth()],
    weekday: WEEKDAYS[d.getDay()],
    year: d.getFullYear(),
  }
}

/** «hoy», «mañana», «en 3 días», «hace 2 días» o «el miércoles 17 sep» para leer una fecha en
 * relación con hoy. */
export function relativeDay(iso: string, today: string): string {
  const a = new Date(`${iso}T12:00:00`).getTime()
  const b = new Date(`${today}T12:00:00`).getTime()
  const diff = Math.round((a - b) / 86_400_000)
  if (diff === 0) return "hoy"
  if (diff === 1) return "mañana"
  if (diff === -1) return "ayer"
  if (diff > 1 && diff < 7) return `en ${diff} días`
  if (diff < -1 && diff > -7) return `hace ${-diff} días`
  const p = dateParts(iso)
  return `el ${p.weekday} ${p.day} ${p.month}`
}

// ---- Agenda: qué sigue ----------------------------------------------------------------------------

export interface AgendaItem {
  id: number
  title: string
  /** YYYY-MM-DD */
  date: string
  /** `entrega` = fecha de entrega de un proyecto; `tarea` = vencimiento de una tarea suelta. */
  kind: "tarea" | "entrega"
  /** La fecha ya pasó y sigue pendiente. */
  late: boolean
  /** «3 de 9 pasos», «para la próxima clase» o null. */
  detail: string | null
}

export interface Agenda {
  items: AgendaItem[]
  /** Pendientes con fecha que no entraron en `items` por el tope. */
  more: number
  /** Pendientes (raíces) sin ninguna fecha. */
  undated: number
}

/** Lo pendiente con fecha, de lo más próximo a lo más lejano: tareas sueltas por su vencimiento y
 * proyectos por su entrega (la mayor entre la suya y la de sus pasos vivos). Lo que pasó y sigue
 * pendiente queda primero, marcado `late`. */
export function agendaOf(tareas: Tarea[], today: string, limit = 5): Agenda {
  const pending = tareas.filter((t) => t.status === "pendiente")
  const { roots, childrenOf } = groupTareas(pending, tareas)
  const items: AgendaItem[] = []
  let undated = 0
  for (const t of roots) {
    const steps = childrenOf.get(t.id) ?? []
    const isProject = t.isParent || steps.length > 0
    if (isProject) {
      const live = steps.filter((s) => s.status !== "cancelada")
      const dues = [t.dueOn, ...live.map((s) => s.dueOn)].filter((d): d is string => !!d)
      if (dues.length === 0) {
        undated += 1
        continue
      }
      const due = dues.reduce((a, b) => (a > b ? a : b))
      const done = live.filter((s) => s.status === "completada").length
      items.push({
        id: t.id,
        title: t.title,
        date: due,
        kind: "entrega",
        late: due < today,
        detail: live.length ? `${done} de ${live.length} pasos` : null,
      })
      continue
    }
    if (!t.dueOn) {
      undated += 1
      continue
    }
    items.push({
      id: t.id,
      title: t.title,
      date: t.dueOn,
      kind: "tarea",
      late: t.dueOn < today,
      detail: t.dueSource === "proxima_sesion" ? "para la próxima clase" : null,
    })
  }
  items.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.id - b.id))
  return { items: items.slice(0, limit), more: Math.max(0, items.length - limit), undated }
}

// ---- Datos: forma del valor --------------------------------------------------------------------------

export type DatoShape = "url" | "code" | "text"

/** Cómo pintar un dato: un enlace se abre, un código (sin espacios, con dígitos, mayúsculas o
 * signos: clave de clase, usuario, teléfono) se copia en mono, el resto es texto. */
export function datoShape(value: string): DatoShape {
  const v = value.trim()
  if (/^https?:\/\/\S+$/i.test(v)) return "url"
  if (v.length <= 48 && !/\s/.test(v) && /[\d@A-Z_\-.:#/+]/.test(v) && !/^[a-záéíóúñü]+$/i.test(v)) {
    return "code"
  }
  return "text"
}

/** «campus.colombobogota.edu.co» o «englishhub.oup.com/course/12» (sin www ni barra final),
 * acotado para que quepa en una fila. */
export function hostOf(url: string, maxPath = 24): string {
  try {
    const u = new URL(url)
    const host = u.hostname.replace(/^www\./, "")
    const path = u.pathname.replace(/\/$/, "")
    if (!path) return host
    return `${host}${path.length > maxPath ? `${path.slice(0, maxPath - 1)}…` : path}`
  } catch {
    return url
  }
}

// ---- Cronología ---------------------------------------------------------------------------------------

export interface MonthGroup {
  key: string
  label: string
  items: AsuntoHito[]
}

/** Agrupa hitos por mes en el orden en que vienen; los sin fecha quedan al final en «sin fecha». */
export function groupHitosByMonth(hitos: AsuntoHito[]): MonthGroup[] {
  const groups: MonthGroup[] = []
  const undated: AsuntoHito[] = []
  for (const h of hitos) {
    if (!h.date) {
      undated.push(h)
      continue
    }
    const key = monthKey(h.date)
    let g = groups.find((x) => x.key === key)
    if (!g) {
      const [y, m] = key.split("-").map(Number)
      g = { key, label: monthLongLabel(new Date(y, m - 1, 1)), items: [] }
      groups.push(g)
    }
    g.items.push(h)
  }
  if (undated.length > 0) groups.push({ key: "sin-fecha", label: "sin fecha", items: undated })
  return groups
}

/** Más reciente primero; los sin fecha al final. */
export function hitosNewestFirst(hitos: AsuntoHito[]): AsuntoHito[] {
  return hitos
    .slice()
    .sort((a, b) => {
      if (!a.date && !b.date) return 0
      if (!a.date) return 1
      if (!b.date) return -1
      return a.date < b.date ? 1 : a.date > b.date ? -1 : 0
    })
}

// ---- Evidencia y elenco --------------------------------------------------------------------------------

/** Los archivos del asunto (diapositivas, programas, PDF adjuntados), más reciente primero. */
export function documentosOf(evidence: AsuntoEvidence[]): AsuntoEvidence[] {
  return evidence
    .filter((e) => e.kind === "documentos")
    .sort((a, b) => (b.occurredAt ?? "").localeCompare(a.occurredAt ?? ""))
}

const PEOPLE_KINDS = new Set(["persona", "organizacion"])

/** Quiénes (personas y organizaciones) vs. el resto de vértices atados (eventos, canales…). */
export function splitElenco(elenco: AsuntoElencoItem[]): {
  people: AsuntoElencoItem[]
  rest: AsuntoElencoItem[]
} {
  return {
    people: elenco.filter((e) => PEOPLE_KINDS.has(e.kind)),
    rest: elenco.filter((e) => !PEOPLE_KINDS.has(e.kind)),
  }
}

/** Resumen corto de las tareas para la cabecera del bloque: «3 pendientes · 1 proyecto · 5 vencidas». */
export function tareasSummary(tareas: Tarea[]): string {
  const abiertas = tareas.filter((t) => t.status === "pendiente")
  const vivas = abiertas.filter((t) => !t.isParent).length
  const proyectos = abiertas.filter((t) => t.isParent).length
  const vencidas = tareas.filter((t) => t.status === "vencida").length
  const cerradas = tareas.filter((t) => t.status === "completada" || t.status === "cancelada").length
  return [
    `${vivas} ${vivas === 1 ? "pendiente" : "pendientes"}`,
    proyectos ? `${proyectos} ${proyectos === 1 ? "proyecto" : "proyectos"}` : null,
    vencidas ? `${vencidas} ${vencidas === 1 ? "vencida" : "vencidas"}` : null,
    cerradas ? `${cerradas} ${cerradas === 1 ? "cerrada" : "cerradas"}` : null,
  ]
    .filter(Boolean)
    .join(" · ")
}

// ---- Archivos ----------------------------------------------------------------------------------------

/** «1,2 MB», «680 KB», «12 B» para el tamaño de un archivo. */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return ""
  if (n < 1024) return `${n} B`
  const kb = n / 1024
  if (kb < 1024) return `${Math.round(kb)} KB`
  const mb = kb / 1024
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1).replace(".", ",") : Math.round(mb)} MB`
  return `${(mb / 1024).toFixed(1).replace(".", ",")} GB`
}
