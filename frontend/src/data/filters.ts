// CRUD de reglas de filtro (filter_rules) contra la API real — gestión desde el dashboard.

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"
import type { FilterAction, FilterRule } from "@/types/domain"

interface FilterApi {
  id: number
  source_type: string | null
  source_id: number | null
  scope: Record<string, unknown>
  action: string
  priority: number
  enabled: boolean
}

function toRule(r: FilterApi): FilterRule {
  return {
    id: r.id,
    sourceType: r.source_type,
    sourceId: r.source_id,
    scope: r.scope,
    action: r.action as FilterAction,
    priority: r.priority,
    enabled: r.enabled,
  }
}

/** Reglas de filtro del usuario (GET /filters). */
export async function fetchFilters(opts?: { sourceType?: string }): Promise<FilterRule[]> {
  const qs = new URLSearchParams()
  if (opts?.sourceType) qs.set("source_type", opts.sourceType)
  const q = qs.toString()
  const res = await apiGet<{ items: FilterApi[] }>(`/filters${q ? `?${q}` : ""}`)
  return res.items.map(toRule)
}

export interface FilterCreate {
  sourceType?: string | null
  sourceId?: number | null
  scope: Record<string, unknown>
  action: FilterAction
  priority?: number
  enabled?: boolean
}

/** Crea una regla (POST /filters). */
export async function createFilter(body: FilterCreate): Promise<FilterRule> {
  return toRule(
    await apiPost<FilterApi>("/filters", {
      source_type: body.sourceType ?? null,
      source_id: body.sourceId ?? null,
      scope: body.scope,
      action: body.action,
      priority: body.priority ?? 100,
      enabled: body.enabled ?? true,
    }),
  )
}

/** Update parcial (PATCH /filters/{id}). */
export async function updateFilter(
  id: number,
  patch: { scope?: Record<string, unknown>; action?: FilterAction; priority?: number; enabled?: boolean },
): Promise<FilterRule> {
  return toRule(await apiPatch<FilterApi>(`/filters/${id}`, patch))
}

/** Borra una regla (DELETE /filters/{id}). */
export async function deleteFilter(id: number): Promise<void> {
  await apiDelete(`/filters/${id}`)
}
