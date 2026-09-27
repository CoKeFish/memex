// Agrupar y ordenar la lista de tareas. Puro: sin DOM, sin red, testeable.
//
// El problema medido en la lista real (26 pendientes): 13 eran del mismo curso y 6 del mismo
// trámite, así que el nombre del asunto se repetía trece veces mientras los pasos de un proyecto
// quedaban desperdigados entre cosas que no tienen nada que ver. Una sola tira de 26 filas no
// contesta «¿qué hago ahora?».
//
// Dos ejes, y el dueño elige cuál manda:
//  - CUÁNDO: atrasadas, hoy, esta semana, más adelante, sin fecha. La vista de «qué hago ahora».
//  - ASUNTO: un bloque por asunto. La vista de «voy a sentarme con el curso».
//
// Los pasos de un proyecto NO se agrupan por su cuenta: viajan con su proyecto, y el proyecto cae
// en el grupo de su ENTREGA (la fecha mayor entre sus pasos vivos), la misma regla que ya usan la
// agenda del día y la columna «qué sigue» de asuntos.

import type { Tarea } from "@/data"

export type TareasGroupBy = "cuando" | "asunto" | "categoria" | "nada"
export type TareasSortBy = "fecha" | "prioridad"

export interface TareaGrupo {
  /** Clave estable para React y para los tests. */
  key: string
  /** Lo que se lee en el separador. */
  label: string
  /** Las raíces del grupo; sus pasos se resuelven en el componente. */
  tareas: Tarea[]
  /** Un grupo que pide atención: hoy se usa solo para lo atrasado. */
  urgent?: boolean
}

const PRIORITY_RANK: Record<string, number> = { alta: 0, normal: 1, baja: 2 }

function today(now: Date): string {
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, "0")
  const d = String(now.getDate()).padStart(2, "0")
  return `${y}-${m}-${d}`
}

function plusDays(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00`)
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

/**
 * La fecha con la que la tarea entra a un grupo y se ordena.
 *
 * Para un proyecto es su ENTREGA: la mayor entre la suya y las de sus pasos vivos. Un proyecto no
 * se puede ordenar por su primer paso, porque entonces un proyecto de tres meses aparecería junto
 * a lo de mañana.
 */
export function fechaDeOrden(t: Tarea, pasos: Tarea[]): string | null {
  const vivos = pasos.filter((p) => p.status !== "cancelada" && p.status !== "completada")
  const fechas = [t.dueOn, ...vivos.map((p) => p.dueOn)].filter((d): d is string => !!d)
  if (!fechas.length) return null
  return vivos.length ? fechas.reduce((a, b) => (a > b ? a : b)) : fechas[0]
}

function compare(
  a: Tarea,
  b: Tarea,
  sortBy: TareasSortBy,
  fechaDe: (t: Tarea) => string | null,
): number {
  if (sortBy === "prioridad") {
    const pa = PRIORITY_RANK[a.priority] ?? 1
    const pb = PRIORITY_RANK[b.priority] ?? 1
    if (pa !== pb) return pa - pb
  }
  const fa = fechaDe(a)
  const fb = fechaDe(b)
  // Sin fecha al final: no es que sea lejana, es que no tiene.
  if (fa === null && fb !== null) return 1
  if (fa !== null && fb === null) return -1
  if (fa !== null && fb !== null && fa !== fb) return fa < fb ? -1 : 1
  if (sortBy === "fecha") {
    const pa = PRIORITY_RANK[a.priority] ?? 1
    const pb = PRIORITY_RANK[b.priority] ?? 1
    if (pa !== pb) return pa - pb
  }
  return a.id - b.id
}

/** Nombre del bucket temporal. `null` = sin fecha. */
export function bucketDeFecha(fecha: string | null, hoy: string): TareaGrupo["key"] {
  if (fecha === null) return "sin-fecha"
  if (fecha < hoy) return "atrasadas"
  if (fecha === hoy) return "hoy"
  if (fecha <= plusDays(hoy, 7)) return "semana"
  return "adelante"
}

const BUCKET_LABEL: Record<string, string> = {
  atrasadas: "Atrasadas",
  hoy: "Hoy",
  semana: "Esta semana",
  adelante: "Más adelante",
  "sin-fecha": "Sin fecha",
}
const BUCKET_ORDER = ["atrasadas", "hoy", "semana", "adelante", "sin-fecha"]

// Orden fijo de las categorías (el mismo del backend, `tareas.CATEGORIES`).
const CATEGORY_ORDER = ["seguridad", "entrega", "tramite", "pago", "inscripcion", "dinamica", "otro"]
const CATEGORY_LABEL: Record<string, string> = {
  seguridad: "Seguridad",
  entrega: "Entregas",
  tramite: "Trámites",
  pago: "Pagos",
  inscripcion: "Inscripciones",
  dinamica: "Dinámicas",
  otro: "Otras",
}

/**
 * Agrupa las RAÍCES (un paso nunca es raíz) y las ordena dentro de cada grupo.
 *
 * `pasosDe` da los pasos de un proyecto para poder calcular su entrega. `now` se inyecta para que
 * los tests no dependan del día en que corren.
 */
export function agruparTareas(
  roots: Tarea[],
  pasosDe: (t: Tarea) => Tarea[],
  { groupBy, sortBy, now = new Date() }: {
    groupBy: TareasGroupBy
    sortBy: TareasSortBy
    now?: Date
  },
): TareaGrupo[] {
  const hoy = today(now)
  const fechaDe = (t: Tarea): string | null => fechaDeOrden(t, pasosDe(t))
  const ordenar = (xs: Tarea[]): Tarea[] =>
    [...xs].sort((a, b) => compare(a, b, sortBy, fechaDe))

  if (groupBy === "nada") {
    return [{ key: "todas", label: "", tareas: ordenar(roots) }]
  }

  if (groupBy === "cuando") {
    const buckets = new Map<string, Tarea[]>()
    for (const t of roots) {
      const k = bucketDeFecha(fechaDe(t), hoy)
      buckets.set(k, [...(buckets.get(k) ?? []), t])
    }
    return BUCKET_ORDER.filter((k) => buckets.has(k)).map((k) => ({
      key: k,
      label: BUCKET_LABEL[k],
      tareas: ordenar(buckets.get(k) ?? []),
      // Solo lo atrasado pide atencion; el resto queda callado a proposito.
      ...(k === "atrasadas" ? { urgent: true } : {}),
    }))
  }

  if (groupBy === "categoria") {
    // Un bloque por categoría en el orden fijo del vocabulario (seguridad primero: un acceso que
    // no fue propio no espera); «otro» al final. Dentro, el mismo orden que el resto.
    const porCat = new Map<string, Tarea[]>()
    for (const t of roots) {
      porCat.set(t.category, [...(porCat.get(t.category) ?? []), t])
    }
    return CATEGORY_ORDER.filter((k) => porCat.has(k)).map((k) => ({
      key: `cat-${k}`,
      label: CATEGORY_LABEL[k],
      tareas: ordenar(porCat.get(k) ?? []),
      ...(k === "seguridad" ? { urgent: true } : {}),
    }))
  }

  // Por asunto: el bloque se ordena por lo más próximo que tenga adentro, y «sin asunto» va al
  // final (no es un asunto, es la ausencia de uno).
  const porAsunto = new Map<string, { label: string; tareas: Tarea[] }>()
  for (const t of roots) {
    const k = t.asuntoId == null ? "" : String(t.asuntoId)
    const label = t.asuntoName ?? "Sin asunto"
    const prev = porAsunto.get(k)
    porAsunto.set(k, { label, tareas: [...(prev?.tareas ?? []), t] })
  }
  const grupos = [...porAsunto.entries()].map(([k, v]) => ({
    key: k || "sin-asunto",
    label: v.label,
    tareas: ordenar(v.tareas),
  }))
  grupos.sort((a, b) => {
    if (a.key === "sin-asunto") return 1
    if (b.key === "sin-asunto") return -1
    const fa = fechaDe(a.tareas[0])
    const fb = fechaDe(b.tareas[0])
    if (fa === null && fb !== null) return 1
    if (fa !== null && fb === null) return -1
    if (fa !== null && fb !== null && fa !== fb) return fa < fb ? -1 : 1
    return a.label.localeCompare(b.label, "es")
  })
  return grupos
}

export const GROUP_BY_LABEL: Record<TareasGroupBy, string> = {
  cuando: "Por cuándo",
  asunto: "Por asunto",
  categoria: "Por categoría",
  nada: "Sin agrupar",
}

export const SORT_BY_LABEL: Record<TareasSortBy, string> = {
  fecha: "Fecha primero",
  prioridad: "Prioridad primero",
}
