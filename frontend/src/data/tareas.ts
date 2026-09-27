// Tareas (router /tareas): lista con filtros y cambio de estado desde el dashboard. Como los demás
// módulos de data/: funciones async + transform snake_case → camelCase.

import { apiGet, apiPost } from "@/lib/api"

export type TareaStatus = "pendiente" | "completada" | "cancelada" | "vencida"
export type TareaPriority = "alta" | "normal" | "baja"
/** De qué clase de cosa es (vocabulario fijo del backend, `tareas.CATEGORIES`). */
export type TareaCategory =
  | "seguridad"
  | "entrega"
  | "tramite"
  | "pago"
  | "inscripcion"
  | "dinamica"
  | "otro"
export const TAREA_CATEGORIES: TareaCategory[] = [
  "seguridad",
  "entrega",
  "tramite",
  "pago",
  "inscripcion",
  "dinamica",
  "otro",
]

export interface Tarea {
  id: number
  title: string
  description: string
  dueOn: string | null
  priority: TareaPriority
  category: TareaCategory
  status: TareaStatus
  asuntoId: number | null
  asuntoName: string | null
  doneAt: string | null
  /** `proxima_sesion` cuando la fecha la puso el sistema con el horario del curso. */
  dueSource: string | null
  origin: string | null
  /** `no_es_tarea` u otra razón cuando el usuario la descartó como corrección. */
  discardReason: string | null
  sourceInboxIds: number[]
  /** Paso de un proyecto: id de la tarea padre. */
  parentId: number | null
  /** Recursos: URLs que hacen falta (plantilla, video, sitio). */
  links: string[]
  /** Tarea padre (un proyecto con pasos). */
  isParent: boolean
  overdue: boolean
  createdAt: string
  updatedAt: string
}

interface TareaApi {
  id: number
  title: string
  description: string
  due_on: string | null
  priority: string
  category?: string
  status: string
  asunto_id: number | null
  asunto_name: string | null
  done_at: string | null
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
  overdue: boolean
  source_inbox_ids?: number[]
  origin?: string
  parent_id?: number | null
}

export function toTarea(a: TareaApi): Tarea {
  const meta = a.metadata ?? {}
  return {
    id: a.id,
    title: a.title,
    description: a.description ?? "",
    dueOn: a.due_on,
    priority: (a.priority as TareaPriority) ?? "normal",
    category: (TAREA_CATEGORIES as string[]).includes(a.category ?? "")
      ? (a.category as TareaCategory)
      : "otro",
    status: a.status as TareaStatus,
    asuntoId: a.asunto_id,
    asuntoName: a.asunto_name,
    doneAt: a.done_at,
    dueSource: typeof meta.due_source === "string" ? meta.due_source : null,
    origin: a.origin ?? (typeof meta.origin === "string" ? meta.origin : null),
    discardReason: typeof meta.discard_reason === "string" ? meta.discard_reason : null,
    sourceInboxIds: a.source_inbox_ids ?? [],
    parentId: a.parent_id ?? null,
    links: Array.isArray(meta.links) ? meta.links.filter((l): l is string => typeof l === "string") : [],
    isParent: meta.is_parent === true,
    overdue: a.overdue,
    createdAt: a.created_at,
    updatedAt: a.updated_at,
  }
}

export interface FetchTareasOpts {
  asuntoId?: number
  status?: TareaStatus | "all"
  overdue?: boolean
  q?: string
  limit?: number
}

export async function fetchTareas(opts: FetchTareasOpts = {}): Promise<Tarea[]> {
  const p = new URLSearchParams()
  p.set("status", opts.status ?? "pendiente")
  if (opts.asuntoId != null) p.set("asunto_id", String(opts.asuntoId))
  if (opts.overdue) p.set("overdue", "true")
  if (opts.q) p.set("q", opts.q)
  p.set("limit", String(opts.limit ?? 500))
  const r = await apiGet<{ count: number; items: TareaApi[] }>(`/tareas?${p.toString()}`)
  return r.items.map(toTarea)
}

export async function setTareaStatus(id: number, status: TareaStatus): Promise<Tarea> {
  return toTarea(await apiPost<TareaApi>(`/tareas/${id}/status`, { status }))
}

/** Colgar como paso de un proyecto (otra tarea) o descolgar con `null`. */
export async function setTareaParent(id: number, parentId: number | null): Promise<Tarea> {
  return toTarea(await apiPost<TareaApi>(`/tareas/${id}/parent`, { parent_id: parentId }))
}

/** Agrupa: raíces (sin padre, con padre fuera del conjunto, o pasos cancelados/descartados,
 * que se listan aparte entre las cerradas) y pasos VIVOS por padre. */
export function groupTareas(
  tareas: Tarea[],
  context: Tarea[] = tareas,
): { roots: Tarea[]; childrenOf: Map<number, Tarea[]> } {
  const ids = new Set(context.map((t) => t.id))
  const childrenOf = new Map<number, Tarea[]>()
  const nested = (t: Tarea): boolean => {
    const pid = t.parentId
    return pid != null && ids.has(pid) && t.status !== "cancelada"
  }
  for (const t of context) {
    const pid = t.parentId
    if (pid == null || !nested(t)) continue
    const list = childrenOf.get(pid) ?? []
    list.push(t)
    childrenOf.set(pid, list)
  }
  const roots = tareas.filter((t) => !nested(t))
  return { roots, childrenOf }
}

/** Corrección: «no es una tarea». Cancela, bloquea que vuelva a nacer y deja feedback. */
export async function discardTarea(id: number, note = ""): Promise<Tarea> {
  return toTarea(await apiPost<TareaApi>(`/tareas/${id}/discard`, { reason: "no_es_tarea", note }))
}
