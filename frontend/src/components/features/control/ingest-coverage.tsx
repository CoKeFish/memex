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
import { fetchInboxCoverage, fetchSources } from "@/data"
import { addDays } from "@/lib/coverage"
import { formatDateOnly } from "@/lib/format"
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

// Tolerancia de fusión: cuántos días SIN items no rompen un tramo (gap_days del endpoint).
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

/** Timeline de ingesta: qué rangos del historial (fecha del mensaje original) ya se ingirieron,
 *  una pista por fuente. Banda sólida = días con mensajes; banda tenue = barrido sin mensajes;
 *  barrita = cursor incremental. Los huecos son lo que falta por traer — el backfill está justo
 *  debajo en la vista. */
export function IngestCoveragePanel() {
  const tz = activeDisplayTz()
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
      Promise.all([fetchInboxCoverage({ tz, gapDays: Number(gapDays), since, until }), fetchSources()]),
    [tz, gapDays, since, until],
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
      // El componente funde por ancho con la misma geometría que los rangos; count no aplica.
      swept: ln.swept.map((s) => ({ start: s.start, end: s.end, count: 0 })),
      marker: ln.cursor
        ? {
            day: ln.cursor.day,
            label:
              `cursor: al día hasta ${formatDateOnly(ln.cursor.day)}` +
              (ln.cursor.summary ? ` · ${ln.cursor.summary}` : ""),
          }
        : undefined,
    }
  })

  return (
    <Panel>
      <PanelHeader
        eyebrow="cobertura · fecha original"
        title="Timeline de ingesta"
        sub="Qué rangos del historial ya se ingirieron, por fuente — fecha del mensaje original; la banda tenue es tiempo barrido donde no había mensajes"
        right={
          <div className="flex flex-wrap items-center justify-end gap-2">
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
              emptyTitle={
                winPreset === "custom" && !desde
                  ? "Elegí la fecha «desde» para acotar la ventana"
                  : "Aún no se ingirió nada"
              }
              emptyHint="Cuando se ingiera historial, acá se ve qué rangos de tiempo quedaron cubiertos y qué huecos faltan."
            />
            {(coverage?.domainMin ?? null) !== null && (
              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <span
                    className="h-2 w-3 rounded-[2px]"
                    style={{ background: "var(--chart-2)" }}
                  />
                  con mensajes guardados
                </span>
                <span className="flex items-center gap-1.5">
                  <span
                    className="h-2 w-3 rounded-[2px]"
                    style={{ background: "var(--chart-2)", opacity: 0.22 }}
                  />
                  barrido sin mensajes (se buscó y no había)
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="h-2.5 w-0.5 rounded bg-foreground/80" />
                  cursor — al día hasta acá
                </span>
                <span>hueco sin nada = falta por traer</span>
              </div>
            )}
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
