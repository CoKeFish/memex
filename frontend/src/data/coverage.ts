// Cobertura temporal (timeline de rangos de días cubiertos) contra la API real.
//
// El shape lanes/ranges es GENÉRICO (espejo de `CoverageOut` en el backend) y hoy lo producen DOS
// endpoints que comparten `toCoverage` + el componente <CoverageTimeline>:
// - GET /inbox/coverage — rangos INGERIDOS por fecha del mensaje original (`occurred_at`).
// - GET /processing/coverage — de lo ingerido, qué días ya están MANEJADOS (timeline de
//   /procesamiento; `swept` ahí significa "día parcial" y `cursor` la frontera del lote).

import { apiGet } from "@/lib/api"
import type { DayRange } from "@/lib/coverage"

// ---- Tipos de dominio (camelCase) ---------------------------------------------------------------

export interface CoverageRange extends DayRange {
  days: number // días de calendario del tramo (end - start + 1)
}

/** Tramo BARRIDO por la ingesta (reclamado por un fetch de rango o incremental), haya o no
 *  mensajes. Distingue "barrí y estaba vacío" de "nunca lo intenté". */
export interface CoverageSpan {
  start: string // inclusive
  end: string // inclusive
  days: number
}

/** Posición del cursor incremental de la fuente: hasta cuándo está al día. */
export interface CoverageCursor {
  at: string // instante (ISO) de la última puesta al día
  day: string // su día en la tz pedida (posición en el eje)
  summary: string // resumen humano del cursor crudo ("" si no se pudo resumir)
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
  swept: CoverageSpan[]
  cursor: CoverageCursor | null
}

export interface Coverage {
  lanes: CoverageLane[]
  domainMin: string | null
  domainMax: string | null
  tz: string
  gapDays: number
}

// ---- Shape crudo de la API (snake_case) ---------------------------------------------------------

interface CursorApi {
  at: string
  day: string
  summary: string
}

interface LaneApi {
  id: number
  label: string
  kind: string
  enabled: boolean
  total: number
  first_day: string | null
  last_day: string | null
  ranges: CoverageRange[]
  swept: CoverageSpan[]
  cursor: CursorApi | null
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
      swept: ln.swept,
      cursor: ln.cursor,
    })),
    domainMin: r.domain_min,
    domainMax: r.domain_max,
    tz: r.tz,
    gapDays: r.gap_days,
  }
}

/** Cobertura de INGESTA (GET /inbox/coverage): qué rangos de fechas de origen ya están guardados,
 *  por fuente. `gapDays` = tolerancia de fusión (días sin items que no rompen un tramo);
 *  `since`/`until` ("YYYY-MM-DD", inclusivos) acotan la ventana del eje. */
export async function fetchInboxCoverage(
  opts: {
    tz?: string
    gapDays?: number
    kind?: string
    sourceId?: number
    since?: string
    until?: string
  } = {},
): Promise<Coverage> {
  const qs = new URLSearchParams()
  if (opts.tz) qs.set("tz", opts.tz)
  if (opts.gapDays !== undefined) qs.set("gap_days", String(opts.gapDays))
  if (opts.kind) qs.set("kind", opts.kind)
  if (opts.sourceId !== undefined) qs.set("source_id", String(opts.sourceId))
  if (opts.since) qs.set("since", opts.since)
  if (opts.until) qs.set("until", opts.until)
  const q = qs.toString()
  return toCoverage(await apiGet<CoverageApi>(`/inbox/coverage${q ? `?${q}` : ""}`))
}

/** Etapa del pipeline contra la que se mide el avance: `any` = cualquier decisión tomada
 *  (resumido ∨ extraído ∨ blacklist); `summarize`/`extract` = avance de ESA etapa (blacklist
 *  cuenta siempre: un blacklisteado jamás pasa por la etapa y no es backlog). */
export type ProcessingCriterion = "any" | "summarize" | "extract"

/** Cobertura de PROCESAMIENTO (GET /processing/coverage): de lo ingerido, qué días ya están
 *  manejados, por fuente. Banda sólida = día completo; `swept` = día parcial; `cursor` =
 *  frontera del lote por ventanas. `total` por lane = mensajes manejados en la ventana. */
export async function fetchProcessingCoverage(
  opts: {
    tz?: string
    gapDays?: number
    kind?: string
    sourceId?: number
    since?: string
    until?: string
    criterion?: ProcessingCriterion
  } = {},
): Promise<Coverage> {
  const qs = new URLSearchParams()
  if (opts.tz) qs.set("tz", opts.tz)
  if (opts.gapDays !== undefined) qs.set("gap_days", String(opts.gapDays))
  if (opts.kind) qs.set("kind", opts.kind)
  if (opts.sourceId !== undefined) qs.set("source_id", String(opts.sourceId))
  if (opts.since) qs.set("since", opts.since)
  if (opts.until) qs.set("until", opts.until)
  if (opts.criterion) qs.set("criterion", opts.criterion)
  const q = qs.toString()
  return toCoverage(await apiGet<CoverageApi>(`/processing/coverage${q ? `?${q}` : ""}`))
}
