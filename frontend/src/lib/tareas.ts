// Etiquetas y tonos del dominio TAREAS compartidos por /tareas y el panel del asunto.

import type { Tone } from "@/lib/status"
import type { TareaCategory, TareaPriority, TareaStatus } from "@/data/tareas"

export const TAREA_STATUSES: TareaStatus[] = ["pendiente", "vencida", "completada", "cancelada"]

export const TAREA_STATUS_LABEL: Record<TareaStatus, string> = {
  pendiente: "Pendiente",
  vencida: "Vencida",
  completada: "Completada",
  cancelada: "Cancelada",
}

/** Vencida = la fecha pasó y el sistema la cerró (no es error ni logro): tono apagado. */
export const TAREA_STATUS_TONE: Record<TareaStatus, Tone> = {
  pendiente: "pending",
  vencida: "filtered",
  completada: "ok",
  cancelada: "neutral",
}

export const TAREA_STATUS_HINT: Record<TareaStatus, string> = {
  pendiente: "Por hacer.",
  vencida: "Pasó su fecha (la clase o sesión ya ocurrió); el sistema la cerró sola. Se puede reabrir.",
  completada: "Marcada como hecha.",
  cancelada: "Se decidió no hacerla; sale del grafo.",
}

export const TAREA_PRIORITY_LABEL: Record<TareaPriority, string> = {
  alta: "alta",
  normal: "normal",
  baja: "baja",
}

/** Categoría: de qué clase de cosa es la tarea. `otro` no se muestra como chip (no dice nada). */
export const TAREA_CATEGORY_LABEL: Record<TareaCategory, string> = {
  seguridad: "Seguridad",
  entrega: "Entrega",
  tramite: "Trámite",
  pago: "Pago",
  inscripcion: "Inscripción",
  dinamica: "Dinámica",
  otro: "Otro",
}

/** Seguridad pide atención (un acceso que no fue propio no espera); el resto es informativo. */
export const TAREA_CATEGORY_TONE: Record<TareaCategory, Tone> = {
  seguridad: "error",
  entrega: "pending",
  tramite: "neutral",
  pago: "pending",
  inscripcion: "neutral",
  dinamica: "neutral",
  otro: "neutral",
}

export const TAREA_CATEGORY_HINT: Record<TareaCategory, string> = {
  seguridad: "Aviso de seguridad de una cuenta (acceso, contraseña, 2FA): verificar que fue uno mismo.",
  entrega: "Algo que producir y enviar: trabajo, ejercicio, documento, video, publicación.",
  tramite: "Papeles, formularios, citas o confirmaciones con una institución o servicio.",
  pago: "Pagar, cobrar o renovar.",
  inscripcion: "Registrarse, postularse o anotarse.",
  dinamica: "Reto o dinámica de un programa (suele dar puntos).",
  otro: "Sin categoría específica.",
}
