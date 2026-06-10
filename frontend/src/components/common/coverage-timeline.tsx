import { useMemo } from "react"
import { cn } from "@/lib/utils"
import { EmptyState } from "@/components/common/data-state"
import { MeasuredBox } from "@/components/common/measured-box"
import {
  axisTicks,
  mergeForWidth,
  segmentPosition,
  type DayDomain,
  type DayRange,
  type VisualSegment,
} from "@/lib/coverage"
import { formatDateOnly, formatInt } from "@/lib/format"

/**
 * Timeline GENÉRICO de cobertura: por cada lane una pista horizontal con los rangos de días
 * cubiertos como bandas sobre un eje temporal común. No sabe de ingesta ni de procesamiento —
 * recibe lanes/ranges puros (hoy lo alimenta /inbox/coverage; mañana cualquier endpoint con el
 * mismo shape). Tooltips con `title` nativo (patrón del repo) — sin estado de hover.
 */
export interface CoverageTimelineLane {
  id: number | string
  label: string
  /** Texto secundario junto al label (p. ej. el medio: correo/chat/social). */
  sublabel?: string
  /** Atenuar la lane (p. ej. fuente deshabilitada). */
  muted?: boolean
  /** Color CSS de los segmentos; default `var(--chart-2)`. */
  color?: string
  total: number
  ranges: DayRange[]
  /** Tramos BARRIDOS (reclamados por la ingesta aunque no hayan dejado items): se pintan como
   *  banda tenue bajo los segmentos sólidos. `count` se ignora (pasar 0). */
  swept?: DayRange[]
}

function fmtSpan(seg: VisualSegment): string {
  return seg.start === seg.end
    ? formatDateOnly(seg.start)
    : `${formatDateOnly(seg.start)} – ${formatDateOnly(seg.end)}`
}

function defaultTooltip(seg: VisualSegment, lane: CoverageTimelineLane): string {
  const base = `${lane.label}\n${fmtSpan(seg)} · ${formatInt(seg.count)} items · ${formatInt(seg.days)} días`
  return seg.merged > 1 ? `${base} · ${seg.merged} tramos` : base
}

function defaultSweptTooltip(seg: VisualSegment, lane: CoverageTimelineLane): string {
  return `${lane.label}\nbarrido: ${fmtSpan(seg)} · ${formatInt(seg.days)} días — ya se buscó acá; donde no hay banda sólida, no había mensajes`
}

export function CoverageTimeline({
  lanes,
  domainMin,
  domainMax,
  emptyTitle = "Sin rangos cubiertos",
  emptyHint,
  formatTooltip = defaultTooltip,
  formatSweptTooltip = defaultSweptTooltip,
}: {
  lanes: CoverageTimelineLane[]
  domainMin: string | null
  domainMax: string | null
  emptyTitle?: string
  emptyHint?: string
  formatTooltip?: (seg: VisualSegment, lane: CoverageTimelineLane) => string
  formatSweptTooltip?: (seg: VisualSegment, lane: CoverageTimelineLane) => string
}) {
  const domain: DayDomain | null = useMemo(
    () => (domainMin && domainMax ? { min: domainMin, max: domainMax } : null),
    [domainMin, domainMax],
  )
  if (!domain) return <EmptyState title={emptyTitle} hint={emptyHint} />

  return (
    <div className="space-y-1.5">
      {lanes.map((lane) => (
        <div key={lane.id} className="flex items-center gap-3">
          <div
            className={cn(
              "flex w-44 shrink-0 items-baseline gap-1.5 text-xs",
              lane.muted && "text-muted-foreground",
            )}
            title={lane.label}
          >
            <span className="truncate">{lane.label}</span>
            {lane.sublabel && (
              <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                {lane.sublabel}
              </span>
            )}
          </div>
          <MeasuredBox className="relative h-6 flex-1">
            {({ w }) => (
              <>
                <div className="absolute inset-x-0 inset-y-1.5 rounded-sm bg-muted/40" />
                {(lane.swept ?? []).length > 0 &&
                  mergeForWidth(lane.swept ?? [], domain, w).map((seg) => {
                    const pos = segmentPosition(seg, domain)
                    return (
                      <div
                        key={`b-${seg.start}`}
                        className="absolute inset-y-1.5 rounded-[2px]"
                        style={{
                          left: `${pos.leftPct}%`,
                          width: `${pos.widthPct}%`,
                          minWidth: 2,
                          background: lane.color ?? "var(--chart-2)",
                          opacity: lane.muted ? 0.1 : 0.22,
                        }}
                        title={formatSweptTooltip(seg, lane)}
                      />
                    )
                  })}
                {mergeForWidth(lane.ranges, domain, w).map((seg) => {
                  const pos = segmentPosition(seg, domain)
                  return (
                    <div
                      key={seg.start}
                      className={cn("absolute inset-y-1.5 rounded-[2px]", lane.muted && "opacity-40")}
                      style={{
                        left: `${pos.leftPct}%`,
                        width: `${pos.widthPct}%`,
                        minWidth: 2,
                        background: lane.color ?? "var(--chart-2)",
                      }}
                      title={formatTooltip(seg, lane)}
                    />
                  )
                })}
              </>
            )}
          </MeasuredBox>
          <div className="num w-16 shrink-0 text-right text-xs text-muted-foreground">
            {formatInt(lane.total)}
          </div>
        </div>
      ))}

      <div className="flex items-center gap-3 pt-0.5">
        <div className="w-44 shrink-0" />
        <MeasuredBox className="relative h-5 flex-1">
          {({ w }) => (
            <>
              {axisTicks(domain, Math.max(4, Math.floor(w / 70))).map((t) => (
                <div
                  key={t.day}
                  className="absolute top-0 flex flex-col items-start"
                  style={{ left: `${t.pct}%` }}
                >
                  <span className="h-1.5 w-px bg-border" />
                  <span className="mt-0.5 whitespace-nowrap text-[10px] leading-none text-muted-foreground">
                    {t.label}
                  </span>
                </div>
              ))}
            </>
          )}
        </MeasuredBox>
        <div className="w-16 shrink-0" />
      </div>
    </div>
  )
}
