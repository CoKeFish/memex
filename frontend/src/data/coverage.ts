// Cobertura temporal (timeline de rangos de días cubiertos) contra la API real.
//
// El shape lanes/ranges es GENÉRICO (espejo de `CoverageOut` en el backend): hoy lo produce solo
// GET /inbox/coverage — rangos INGERIDOS por fecha del mensaje original (`occurred_at`) — pero una
// futura vista de procesamiento puede exponer su propio endpoint con el mismo shape y reusar
// `toCoverage` + el componente <CoverageTimeline> tal cual.

import { apiGet } from "@/lib/api"
import type { DayRange } from "@/lib/coverage"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface CoverageRange extends DayRange {
  days: number // días de calendario del tramo (end - start + 1)
}

export interface CoverageLane {
  id: number
  label: string // sources.name (el caller puede preferir una etiqueta amigable propia)
  kind: string // email | chat | social | other
  enabled: boolean
  total: number
  firstDay: string | null
  lastDay: string | null
  ranges: CoverageRange[]
}

export interface Coverage {
  lanes: CoverageLane[]
  domainMin: string | null
  domainMax: string | null
  tz: string
  gapDays: number
}

// ---- Shape crudo de la API (snake_case) ---------------------------------------------------------

interface LaneApi {
  id: number
  label: string
  kind: string
  enabled: boolean
  total: number
  first_day: string | null
  last_day: string | null
  ranges: CoverageRange[]
}

interface CoverageApi {
  lanes: LaneApi[]
  domain_min: string | null
  domain_max: string | null
  tz: string
  gap_days: number
}

/** Transform genérico API → dominio. Exportado para que cualquier endpoint futuro con el mismo
 *  shape (p. ej. cobertura de procesamiento) lo reuse sin duplicarlo. */
export function toCoverage(r: CoverageApi): Coverage {
  return {
    lanes: r.lanes.map((ln) => ({
      id: ln.id,
      label: ln.label,
      kind: ln.kind,
      enabled: ln.enabled,
      total: ln.total,
      firstDay: ln.first_day,
      lastDay: ln.last_day,
      ranges: ln.ranges,
    })),
    domainMin: r.domain_min,
    domainMax: r.domain_max,
    tz: r.tz,
    gapDays: r.gap_days,
  }
}

/** Cobertura de INGESTA (GET /inbox/coverage): qué rangos de fechas de origen ya están guardados,
 *  por fuente. `gapDays` = tolerancia de fusión (días sin items que no rompen un tramo). */
export async function fetchInboxCoverage(
  opts: { tz?: string; gapDays?: number; kind?: string; sourceId?: number } = {},
): Promise<Coverage> {
  const qs = new URLSearchParams()
  if (opts.tz) qs.set("tz", opts.tz)
  if (opts.gapDays !== undefined) qs.set("gap_days", String(opts.gapDays))
  if (opts.kind) qs.set("kind", opts.kind)
  if (opts.sourceId !== undefined) qs.set("source_id", String(opts.sourceId))
  const q = qs.toString()
  return toCoverage(await apiGet<CoverageApi>(`/inbox/coverage${q ? `?${q}` : ""}`))
}
