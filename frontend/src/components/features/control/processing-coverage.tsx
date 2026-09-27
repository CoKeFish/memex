import { useState } from "react"
import { ErrorState, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import {
  CoverageTimeline,
  type CoverageTimelineLane,
} from "@/components/common/coverage-timeline"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { fetchProcessingCoverage, fetchSources, type ProcessingCriterion } from "@/data"
import { addDays, type VisualSegment } from "@/lib/coverage"
import { formatDateOnly, formatInt } from "@/lib/format"
import { sourceFullLabel } from "@/lib/inbox-format"
import { activeDisplayTz, todayInTz } from "@/lib/timezone"
import { useAsync } from "@/lib/use-async"

// Color por medio (mismas series que el resto de charts) + etiqueta corta para la lane.
const KIND_COLOR: Record<string, string> = {
  email: "var(--chart-2)",
  chat: "var(--chart-5)",
  social: "var(--chart-4)",
  other: "var(--chart-1)",
}
const KIND_LABEL: Record<string, string> = {
  email: "correo",
  chat: "chat",
  social: "social",
  other: "otro",
}

// Etapa contra la que se mide el avance ("manejado"). Blacklist cuenta siempre como decisión.
const CRITERION_OPTIONS: { value: ProcessingCriterion; label: string }[] = [
  { value: "any", label: "Todo (cualquier etapa)" },
  { value: "summarize", label: "Resumen" },
  { value: "extract", label: "Extracción" },
]

// Tolerancia de fusión: cuántos días SIN mensajes no rompen un tramo (gap_days del endpoint).
// Un día con pendientes SIEMPRE corta la banda sólida, tenga la tolerancia que tenga.
const GAP_OPTIONS = [
  { value: "0", label: "Estricto (sin huecos)" },
  { value: "2", label: "Huecos ≤ 2 días" },
  { value: "7", label: "Huecos ≤ 7 días" },
]

// Ventana del eje: presets relativos a hoy (TZ display) o desde–hasta libre.
const WINDOW_OPTIONS = [
  { value: "all", label: "Todo el historial" },
  { value: "1y", label: "Último año" },
  { value: "90d", label: "Últimos 90 días" },
  { value: "custom", label: "Personalizado" },
]

function todayStr(tz: string): string {
  const { y, m, d } = todayInTz(tz)
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`
}

function fmtSpan(seg: VisualSegment): string {
  return seg.start === seg.end
    ? formatDateOnly(seg.start)
    : `${formatDateOnly(seg.start)} – ${formatDateOnly(seg.end)}`
}

function fullTooltip(seg: VisualSegment, lane: CoverageTimelineLane): string {
  const base = `${lane.label}\n${fmtSpan(seg)} · ${formatInt(seg.count)} mensajes manejados · ${formatInt(seg.days)} días — todo lo ingerido acá ya está manejado`
  return seg.merged > 1 ? `${base} · ${seg.merged} tramos` : base
}

function partialTooltip(seg: VisualSegment, lane: CoverageTimelineLane): string {
  return `${lane.label}\nparcial: ${fmtSpan(seg)} · ${formatInt(seg.days)} días — acá hay mensajes manejados y pendientes mezclados`
}

/** Timeline de procesamiento: de lo INGERIDO (fecha del mensaje original), qué días ya están
 *  manejados según la etapa elegida — resumido/extraído/blacklist. Banda sólida = día completo;
 *  banda tenue = día parcial; barrita = frontera del lote por ventanas. Solo visualización:
 *  acá no se dispara ningún procesamiento (las corridas van en los paneles de arriba). */
export function ProcessingCoveragePanel() {
  const tz = activeDisplayTz()
  const [criterion, setCriterion] = useState<ProcessingCriterion>("any")
  const [gapDays, setGapDays] = useState("2")
  const [winPreset, setWinPreset] = useState("all")
  const [desde, setDesde] = useState("")
  const [hasta, setHasta] = useState("")

  const hoy = todayStr(tz)
  let since: string | undefined
  let until: string | undefined
  if (winPreset === "1y") {
    since = addDays(hoy, -365)
    until = hoy
  } else if (winPreset === "90d") {
    since = addDays(hoy, -90)
    until = hoy
  } else if (winPreset === "custom" && desde) {
    since = desde
    until = hasta || hoy
  }

  const st = useAsync(
    () =>
      Promise.all([
        fetchProcessingCoverage({ tz, gapDays: Number(gapDays), since, until, criterion }),
        fetchSources(),
      ]),
    [tz, gapDays, since, until, criterion],
  )

  const [coverage, sources] = st.data ?? [null, null]
  const lanes: CoverageTimelineLane[] = (coverage?.lanes ?? []).map((ln) => {
    const src = sources?.find((s) => s.id === ln.id)
    return {
      id: ln.id,
      label: src ? sourceFullLabel(src) : ln.label,
      sublabel: KIND_LABEL[ln.kind] ?? ln.kind,
      muted: !ln.enabled,
      color: KIND_COLOR[ln.kind] ?? KIND_COLOR.other,
      total: ln.total,
      ranges: ln.ranges,
      // Mismo slot visual que el barrido de ingesta, semántica propia: días PARCIALES.
      swept: ln.swept.map((s) => ({ start: s.start, end: s.end, count: 0 })),
      marker: ln.cursor
        ? {
            day: ln.cursor.day,
            label:
              `frontera del lote: procesado hasta ${formatDateOnly(ln.cursor.day)}` +
              (ln.cursor.summary ? ` · ${ln.cursor.summary}` : ""),
          }
        : undefined,
    }
  })

  return (
    <Panel>
      <PanelHeader
        eyebrow="cobertura · fecha original"
        title="Timeline de procesamiento"
        sub="De lo que ya se ingirió, qué días están digeridos según la etapa elegida — solo visualización; las corridas se disparan en los paneles de arriba"
        right={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Select
              value={criterion}
              onValueChange={(v) => setCriterion(v as ProcessingCriterion)}
            >
              <SelectTrigger className="h-8 w-44 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CRITERION_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={winPreset}
              onValueChange={(v) => {
                setWinPreset(v)
                if (v === "custom" && !hasta) setHasta(hoy)
              }}
            >
              <SelectTrigger className="h-8 w-40 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOW_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {winPreset === "custom" && (
              <>
                <Input
                  type="date"
                  value={desde}
                  max={hasta || hoy}
                  onChange={(e) => setDesde(e.target.value)}
                  className="h-8 w-36 text-xs"
                  aria-label="Desde"
                />
                <Input
                  type="date"
                  value={hasta}
                  min={desde || undefined}
                  onChange={(e) => setHasta(e.target.value)}
                  className="h-8 w-36 text-xs"
                  aria-label="Hasta"
                />
              </>
            )}
            <Select value={gapDays} onValueChange={setGapDays}>
              <SelectTrigger className="h-8 w-44 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {GAP_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        }
      />
      <PanelBody>
        {st.loading && !st.data ? (
          <TableSkeleton rows={4} cols={3} />
        ) : st.error ? (
          <ErrorState detail={st.error} onRetry={st.reload} />
        ) : (
          <>
            <CoverageTimeline
              lanes={lanes}
              domainMin={coverage?.domainMin ?? null}
              domainMax={coverage?.domainMax ?? null}
              formatTooltip={fullTooltip}
              formatSweptTooltip={partialTooltip}
              emptyTitle={
                winPreset === "custom" && !desde
                  ? "Elegí la fecha «desde» para acotar la ventana"
                  : "Aún no hay nada manejado en la ventana"
              }
              emptyHint="Cuando el procesamiento avance (corridas, lote por ventanas o blacklist), acá se ve qué días del historial ya quedaron digeridos."
            />
            {(coverage?.domainMin ?? null) !== null && (
              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <span
                    className="h-2 w-3 rounded-[2px]"
                    style={{ background: "var(--chart-2)" }}
                  />
                  día completo — todo lo ingerido está manejado
                </span>
                <span className="flex items-center gap-1.5">
                  <span
                    className="h-2 w-3 rounded-[2px]"
                    style={{ background: "var(--chart-2)", opacity: 0.22 }}
                  />
                  día parcial — quedan pendientes
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="h-2.5 w-0.5 rounded bg-foreground/80" />
                  frontera del lote — procesado hasta acá
                </span>
                <span>hueco = backlog o sin mensajes (la ingesta se ve en Carga) · nº = manejados</span>
              </div>
            )}
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
