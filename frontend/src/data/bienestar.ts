// Superficie de BIENESTAR contra la API real (router /bienestar, solo lectura). Como `finance.ts`:
// funciones async + transform snake_case → camelCase. La escritura va por la CLI/agente, no por acá.

import { apiGet } from "@/lib/api"
import { activeDisplayTz } from "@/lib/timezone"

export interface BienestarRegistro {
  id: number
  category: string
  activity: string
  occurredAt: string
  description: string
  eventId: string | null
}

interface RegistroApiRow {
  id: number
  category: string
  activity: string
  occurred_at: string
  occurred_at_precision: string
  description: string
  detail: Record<string, unknown>
  metadata: Record<string, unknown>
  event_id: string | null
  created_at: string
}

export async function fetchBienestarRegistros(limit = 200): Promise<BienestarRegistro[]> {
  const res = await apiGet<{ items: RegistroApiRow[] }>(`/bienestar/registros?limit=${limit}`)
  return res.items.map((r) => ({
    id: r.id,
    category: r.category,
    activity: r.activity,
    occurredAt: r.occurred_at,
    description: r.description,
    eventId: r.event_id,
  }))
}

export interface BienestarSummary {
  total: number
  byCategory: Record<string, number>
  byActivity: Record<string, number>
}

export async function fetchBienestarSummary(): Promise<BienestarSummary> {
  const r = await apiGet<{
    total: number
    by_category: Record<string, number>
    by_activity: Record<string, number>
  }>("/bienestar/summary")
  return { total: r.total, byCategory: r.by_category, byActivity: r.by_activity }
}

export interface BienestarHabitPoint {
  period: string
  count: number
  met: boolean
}

export interface BienestarHabit {
  id: number
  name: string
  cadence: string
  targetCount: number
  current: number
  metCurrent: boolean
  streak: number
  history: BienestarHabitPoint[]
}

interface HabitApiRow {
  habit: { id: number; name: string }
  cadence: string
  target_count: number
  current: number
  met_current: boolean
  streak: number
  history: BienestarHabitPoint[]
}

/** Hábitos activos con su adherencia (racha + historia), en la TZ de display activa. */
export async function fetchBienestarHabits(periods = 14): Promise<BienestarHabit[]> {
  const qs = new URLSearchParams({ periods: String(periods), tz: activeDisplayTz() })
  const res = await apiGet<{ items: HabitApiRow[] }>(`/bienestar/habits?${qs.toString()}`)
  return res.items.map((h) => ({
    id: h.habit.id,
    name: h.habit.name,
    cadence: h.cadence,
    targetCount: h.target_count,
    current: h.current,
    metCurrent: h.met_current,
    streak: h.streak,
    history: h.history,
  }))
}
