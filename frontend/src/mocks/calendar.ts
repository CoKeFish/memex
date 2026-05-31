import { Rng } from "@/lib/rng"
import type {
  CalendarConflict,
  CalendarOrigin,
  CalendarRawMember,
  CalendarSyncRun,
  ConsolidatedEvent,
  DedupDecision,
} from "@/types/domain"
import { inbox, NOW } from "./index"

const rng = new Rng(602214)

let memberId = 1000
function buildMembers(origins: CalendarOrigin[]): CalendarRawMember[] {
  const winner = origins.includes("provider") ? "provider" : origins[0]
  return origins.map((o) => ({
    id: memberId++,
    origin: o,
    provider: o === "provider" ? "google" : null,
    sourceInboxIds: o === "extraction" ? [rng.int(1, inbox.length)] : [],
    evidence: o === "extraction" ? "Fecha/lugar citados en el mensaje de origen." : "",
    processingOutcome: o === winner ? "unique" : "duplicate",
    isWinner: o === winner,
  }))
}

function d(month: number, day: number): string {
  return `2026-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`
}

interface Tpl {
  title: string
  start: string | null
  end: string | null
  location: string
  origins: CalendarOrigin[]
  protected?: boolean
  rank?: number
  days?: number // multi-día
}

// Plantillas de eventos; se reparten en mayo–julio 2026 (NOW = 2026-05-31).
const TPLS: Tpl[] = [
  { title: "Parcial de Sistemas", start: "09:00", end: "11:00", location: "Aula 204", origins: ["extraction"] },
  { title: "Examen final de BD", start: "09:00", end: "11:00", location: "Aula 110", origins: ["extraction"] },
  { title: "Reunión de seguimiento", start: "10:00", end: "11:00", location: "Google Meet", origins: ["provider"] },
  { title: "Clase de Cálculo", start: "07:00", end: "09:00", location: "Aula 305", origins: ["provider"] },
  { title: "Entrega proyecto final", start: null, end: null, location: "", origins: ["extraction"] },
  { title: "Vuelo BOG → MEX", start: "08:30", end: "11:45", location: "Aeropuerto El Dorado", origins: ["module"], protected: true, rank: 100 },
  { title: "Defensa de tesis", start: "10:00", end: "12:00", location: "Aula Magna", origins: ["provider", "extraction"], protected: true, rank: 90 },
  { title: "Entrevista laboral (remota)", start: "11:00", end: "11:45", location: "Google Meet", origins: ["provider"], rank: 70 },
  { title: "Dentista — control", start: "09:00", end: "10:00", location: "Clínica Norte", origins: ["module"], protected: true, rank: 60 },
  { title: "Hackathon LATAM 2026", start: null, end: null, location: "Aula Magna", origins: ["provider", "extraction"], days: 2 },
  { title: "Cena familiar", start: "14:00", end: "17:00", location: "Casa de Ana", origins: ["extraction"] },
  { title: "Cumpleaños de Ana", start: null, end: null, location: "", origins: ["provider"] },
  { title: "Pago de matrícula", start: null, end: null, location: "", origins: ["extraction"] },
  { title: "Gym", start: "18:00", end: "19:00", location: "SmartFit", origins: ["provider"] },
  { title: "Cita médica", start: "16:00", end: "16:40", location: "IMSS", origins: ["module"] },
  { title: "Viaje a Oaxaca", start: null, end: null, location: "Oaxaca", origins: ["provider"], days: 3 },
  { title: "Junta de equipo", start: "12:00", end: "13:00", location: "Google Meet", origins: ["provider"] },
  { title: "Taller de Rust", start: "17:00", end: "19:00", location: "Lab 2", origins: ["extraction"] },
]

let eid = 1
const events: ConsolidatedEvent[] = []
for (const t of TPLS) {
  const copies = rng.int(1, 2)
  for (let c = 0; c < copies; c++) {
    const month = rng.pick([5, 6, 7])
    const day = rng.int(2, 27)
    const start = d(month, day)
    const end = t.days ? d(month, Math.min(28, day + t.days - 1)) : null
    events.push({
      id: eid++,
      title: t.title,
      startsOn: start,
      endsOn: end,
      startTime: t.start,
      endTime: t.end,
      location: t.location,
      description: "",
      memberCount: t.origins.length,
      origins: t.origins,
      protected: t.protected ?? false,
      priorityRank: t.rank ?? (t.protected ? 80 : 0),
      members: buildMembers(t.origins),
    })
  }
}
export const consolidatedEvents = events.sort((a, b) => a.startsOn.localeCompare(b.startsOn))

// Decisiones de dedup: cómo se resolvió cada par candidato (automático LLM o manual).
export const dedupDecisions: DedupDecision[] = [
  {
    id: 1,
    a: { id: 31, title: "Cena de fin de año", startsOn: "2026-06-20", startTime: "21:00", location: "Casa de Ana", origin: "extraction", provider: null },
    b: { id: 32, title: "Cena fin de año 🎉", startsOn: "2026-06-20", startTime: null, location: "", origin: "provider", provider: "google" },
    reason: "Mismo título aproximado y misma fecha; hora difiere",
    score: 0.86,
    status: "confirmed",
    decidedBy: "llm",
    confidence: 0.91,
    rationale: "Son el mismo evento; la hora del extraído (21:00) gana sobre el provider sin hora. Fusionados.",
    decidedAt: "2026-06-18T03:12:00Z",
  },
  {
    id: 2,
    a: { id: 11, title: "Parcial de Sistemas", startsOn: "2026-06-09", startTime: "09:00", location: "Aula 204", origin: "extraction", provider: null },
    b: { id: 12, title: "Examen Sistemas Operativos", startsOn: "2026-06-09", startTime: "09:00", location: "Aula 204", origin: "extraction", provider: null },
    reason: "Misma fecha, hora y lugar; títulos similares",
    score: 0.74,
    status: "rejected",
    decidedBy: "llm",
    confidence: 0.7,
    rationale: "Materias distintas (Sistemas vs Sistemas Operativos) pese al mismo aula/horario. Se mantienen separados.",
    decidedAt: "2026-06-07T22:40:00Z",
  },
  {
    id: 3,
    a: { id: 41, title: "Reunión de seguimiento", startsOn: "2026-05-22", startTime: "10:00", location: "Google Meet", origin: "provider", provider: "google" },
    b: { id: 42, title: "Seguimiento semanal", startsOn: "2026-05-22", startTime: "10:00", location: "Meet", origin: "extraction", provider: null },
    reason: "Misma fecha y hora; lugar equivalente",
    score: 0.81,
    status: "confirmed",
    decidedBy: "manual",
    confidence: null,
    rationale: "Confirmado a mano: es la misma reunión semanal.",
    decidedAt: "2026-05-21T15:05:00Z",
  },
  {
    id: 4,
    a: { id: 51, title: "Clase de Cálculo", startsOn: "2026-06-03", startTime: "07:00", location: "Aula 305", origin: "provider", provider: "google" },
    b: { id: 52, title: "Cálculo I", startsOn: "2026-06-03", startTime: "07:00", location: "Aula 305", origin: "provider", provider: "google" },
    reason: "Mismo horario y lugar; alias del nombre",
    score: 0.78,
    status: "candidate",
    decidedBy: null,
    confidence: null,
    rationale: null,
    decidedAt: null,
  },
  {
    id: 5,
    a: { id: 61, title: "Gym", startsOn: "2026-06-11", startTime: "18:00", location: "SmartFit", origin: "provider", provider: "google" },
    b: { id: 62, title: "Entrenamiento", startsOn: "2026-06-11", startTime: "18:30", location: "SmartFit", origin: "extraction", provider: null },
    reason: "Mismo lugar y fecha; hora cercana",
    score: 0.66,
    status: "rejected",
    decidedBy: "manual",
    confidence: null,
    rationale: "Marcado distinto a mano: son dos bloques distintos ese día.",
    decidedAt: "2026-06-10T09:00:00Z",
  },
]

// Conflictos: dos eventos distintos de alta importancia que se solapan.
export const calendarConflicts: CalendarConflict[] = [
  {
    id: 1,
    a: { id: 6, title: "Vuelo BOG → MEX", startsOn: "2026-06-12", endsOn: null, startTime: "08:30", endTime: "11:45", location: "Aeropuerto El Dorado", priorityRank: 100, protected: true },
    b: { id: 9, title: "Dentista — control", startsOn: "2026-06-12", endsOn: null, startTime: "09:00", endTime: "10:00", location: "Clínica Norte", priorityRank: 60, protected: true },
    reason: "Solape 09:00–10:00; ambos de alta importancia",
    status: "pending",
    createdAt: "2026-05-30T08:00:00Z",
  },
  {
    id: 2,
    a: { id: 7, title: "Defensa de tesis", startsOn: "2026-06-18", endsOn: null, startTime: "10:00", endTime: "12:00", location: "Aula Magna", priorityRank: 90, protected: true },
    b: { id: 8, title: "Entrevista laboral (remota)", startsOn: "2026-06-18", endsOn: null, startTime: "11:00", endTime: "11:45", location: "Google Meet", priorityRank: 70, protected: false },
    reason: "Solape 11:00–11:45; ambos de alta importancia",
    status: "resolved",
    createdAt: "2026-05-28T12:00:00Z",
  },
]

// Corridas de sync con el proveedor (ingress/egress).
const MIN = 60_000
const HOUR = 3_600_000
function iso(msAgo: number): string {
  return new Date(NOW.getTime() - msAgo).toISOString()
}
export const calendarSyncRuns: CalendarSyncRun[] = [
  { id: 1, account: "google · Personal", direction: "ingress", pulled: 568, created: 12, modified: 4, deleted: 1, unchanged: 551, dedupPairs: 3, errors: 0, status: "ok", startedAt: iso(22 * MIN), finishedAt: iso(22 * MIN - 9000) },
  { id: 2, account: "google · Personal", direction: "egress", pulled: 0, created: 2, modified: 1, deleted: 0, unchanged: 0, dedupPairs: 0, errors: 0, status: "ok", startedAt: iso(22 * MIN), finishedAt: iso(22 * MIN - 4000) },
  { id: 3, account: "google · Personal", direction: "ingress", pulled: 540, created: 3, modified: 1, deleted: 0, unchanged: 536, dedupPairs: 1, errors: 0, status: "ok", startedAt: iso(8 * HOUR), finishedAt: iso(8 * HOUR - 8000) },
  { id: 4, account: "google · Universidad", direction: "ingress", pulled: 0, created: 0, modified: 0, deleted: 0, unchanged: 0, dedupPairs: 0, errors: 1, status: "error", startedAt: iso(30 * HOUR), finishedAt: iso(30 * HOUR - 3000) },
]
