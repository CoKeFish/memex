// Etiquetas, tonos y frases del dominio ASUNTOS compartidos por la lista (/asuntos) y la storyline
// (/asuntos/:id). Los valores crudos (kind/status/method/relation_type) son los del backend.

import type { Tone } from "@/lib/status"
import type {
  AsuntoEvidenceMethod,
  AsuntoKind,
  AsuntoPriority,
  AsuntoRelationType,
  AsuntoStatus,
} from "@/data/asuntos"

export const ASUNTO_KINDS: AsuntoKind[] = [
  "evento",
  "programa",
  "hackathon",
  "tramite",
  "proyecto",
  "comunidad",
  "servicio",
  "otro",
]

export const ASUNTO_KIND_LABEL: Record<AsuntoKind, string> = {
  evento: "Evento",
  programa: "Programa",
  hackathon: "Hackatón",
  tramite: "Trámite",
  proyecto: "Proyecto",
  comunidad: "Comunidad",
  servicio: "Servicio",
  otro: "Otro",
}

export const ASUNTO_STATUSES: AsuntoStatus[] = ["abierto", "candidato", "cerrado", "inactivo"]

export const ASUNTO_STATUS_LABEL: Record<AsuntoStatus, string> = {
  abierto: "Abierto",
  candidato: "Candidato",
  cerrado: "Cerrado",
  inactivo: "Inactivo",
}

/** Plural para los chips de filtro. */
export const ASUNTO_STATUS_PLURAL: Record<AsuntoStatus, string> = {
  abierto: "Abiertos",
  candidato: "Candidatos",
  cerrado: "Cerrados",
  inactivo: "Inactivos",
}

export const ASUNTO_STATUS_TONE: Record<AsuntoStatus, Tone> = {
  abierto: "ok",
  candidato: "review",
  cerrado: "neutral",
  inactivo: "pending",
}

export const ASUNTO_PRIORITIES: AsuntoPriority[] = ["alta", "normal", "baja"]

export const ASUNTO_PRIORITY_LABEL: Record<AsuntoPriority, string> = {
  alta: "Alta",
  normal: "Normal",
  baja: "Baja",
}

/** Qué significa cada prioridad (tooltip de los chips). */
export const ASUNTO_PRIORITY_HINT: Record<AsuntoPriority, string> = {
  alta: "plazo cercano, plata o un derecho en juego, requisito de algo mayor, participación activa",
  normal: "hilo vivo sin urgencia",
  baja: "seguimiento pasivo: comunidad, suscripción sin problema",
}

export const ASUNTO_PRIORITY_TONE: Record<AsuntoPriority, Tone> = {
  alta: "review",
  normal: "neutral",
  baja: "pending",
}

export const ASUNTO_METHOD_LABEL: Record<AsuntoEvidenceMethod, string> = {
  llm: "llm",
  hilo: "hilo",
  evento: "evento",
  asunto: "asunto",
  manual: "manual",
  agent: "agente",
  curator: "curador",
}

/** Frase de una relación vista desde el asunto actual. `out` = este es el origen (src). */
export function relationPhrase(type: AsuntoRelationType, direction: "out" | "in"): string {
  if (direction === "out") {
    return { parte_de: "es parte de", requisito_de: "es requisito de", continua: "continúa en" }[type]
  }
  return { parte_de: "contiene", requisito_de: "requiere", continua: "continúa a" }[type]
}

/** Opciones del formulario «relacionar»: frase + cómo se traduce a (src, dst). */
export const RELATION_OPTIONS: {
  key: string
  label: string
  type: AsuntoRelationType
  direction: "out" | "in"
}[] = [
  { key: "parte_de:out", label: "es parte de", type: "parte_de", direction: "out" },
  { key: "requisito_de:out", label: "es requisito de", type: "requisito_de", direction: "out" },
  { key: "continua:out", label: "continúa en", type: "continua", direction: "out" },
  { key: "parte_de:in", label: "contiene", type: "parte_de", direction: "in" },
  { key: "requisito_de:in", label: "requiere", type: "requisito_de", direction: "in" },
  { key: "continua:in", label: "continúa a", type: "continua", direction: "in" },
]
