import { useLayoutEffect, useRef, useState } from "react"
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts"
import { AlertTriangle } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Badge } from "@/components/ui/badge"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Skeleton } from "@/components/ui/skeleton"
import { formatInt } from "@/lib/format"
import { fetchLogStats, type LogsQuery } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { LogLevel, LogStats } from "@/types/domain"

// Color por nivel para el corte byLevel (mismos tokens de status que la fila de log).
const LEVEL_CLS: Record<LogLevel, string> = {
  debug: "text-muted-foreground",
  info: "text-status-ok",
  warning: "text-status-review",
  error: "text-status-error",
  critical: "text-status-error",
}

const hourMinFmt = new Intl.DateTimeFormat("es-MX", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })

/** Etiqueta del bucket del histograma: instante ISO → "02 jun 14:30" (en TZ del navegador). */
function bucketLabel(iso: string): string {
  return hourMinFmt.format(new Date(iso))
}

interface HistRow {
  label: string
  total: number
  errors: number
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function HistTooltip({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="eyebrow mb-1.5">{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-[2px]" style={{ background: p.color }} />
            {p.dataKey === "errors" ? "Errores" : "Total"}
          </span>
          <span className="num">{formatInt(p.value ?? 0)}</span>
        </div>
      ))}
    </div>
  )
}

/** Histograma temporal (total vs errores) con dimensiones MEDIDAS — sin `ResponsiveContainer`, así
 *  nunca dispara el warning width(-1)/height(-1) de Recharts (mismo patrón que cost-trend.tsx).
 *  `useLayoutEffect` mide antes del paint; un ResizeObserver lo mantiene responsivo. */
function HistChart({ data }: { data: HistRow[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const { width, height } = el.getBoundingClientRect()
      if (width > 0 && height > 0) setSize({ w: width, h: height })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <div ref={ref} className="h-48 w-full">
      {size && (
        <AreaChart width={size.w} height={size.h} data={data} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            minTickGap={28}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            width={40}
            allowDecimals={false}
          />
          <Tooltip content={<HistTooltip />} />
          <Area
            type="monotone"
            dataKey="total"
            stroke="var(--status-ok)"
            fill="var(--status-ok)"
            fillOpacity={0.14}
            strokeWidth={1.5}
          />
          <Area
            type="monotone"
            dataKey="errors"
            stroke="var(--status-error)"
            fill="var(--status-error)"
            fillOpacity={0.2}
            strokeWidth={1.5}
          />
        </AreaChart>
      )}
    </div>
  )
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="eyebrow mb-1">{label}</div>
      <div className={cn("num text-xl font-semibold", tone)}>{value}</div>
    </div>
  )
}

/** Una lista compacta de cortes (nivel/evento/logger) con conteo a la derecha. */
function CountList({
  title,
  rows,
}: {
  title: string
  rows: { key: string; label: string; count: number; cls?: string }[]
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="eyebrow mb-2">{title}</div>
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">—</p>
      ) : (
        <ul className="space-y-1">
          {rows.map((r) => (
            <li key={r.key} className="flex items-center justify-between gap-3 text-xs">
              <span className={cn("truncate font-mono", r.cls)} title={r.label}>
                {r.label}
              </span>
              <span className="num shrink-0 text-muted-foreground">{formatInt(r.count)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Panel de métricas del stream (arriba de EventStream): KPIs + histograma + cortes por
 *  nivel/evento/logger, todo del rango y filtros vigentes. Sin caps silenciosos: si el sink
 *  descartó eventos por overflow de su cola, lo señala con un chip de advertencia. */
export function LogMetrics({ query }: { query: LogsQuery }) {
  const { data, loading, error, reload } = useAsync<LogStats>(
    () => fetchLogStats(query),
    [
      query.tz,
      query.since,
      query.until,
      JSON.stringify(query.level),
      query.levelMode,
      JSON.stringify(query.event),
      query.eventMode,
      JSON.stringify(query.logger),
      query.loggerMode,
      query.requestId,
      query.runId,
      query.sourceId,
      query.inboxId,
      query.q,
    ],
  )

  if (error) {
    return (
      <Panel>
        <ErrorState detail={error} onRetry={reload} />
      </Panel>
    )
  }
  if (loading && !data) {
    return (
      <Panel>
        <PanelHeader eyebrow="Métricas · log_events" title="Resumen del stream" />
        <PanelBody className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
          <Skeleton className="h-48 w-full" />
        </PanelBody>
      </Panel>
    )
  }
  if (!data) return null

  const hist: HistRow[] = data.histogram.map((h) => ({
    label: bucketLabel(h.bucket),
    total: h.total,
    errors: h.errors,
  }))

  const lat = data.latency
  const hasLatency = lat.p50 != null || lat.p95 != null || lat.p99 != null
  const fmtMs = (v: number | null): string => (v == null ? "—" : `${Math.round(v)} ms`)

  return (
    <Panel>
      <PanelHeader
        eyebrow="Métricas · log_events"
        title="Resumen del stream"
        sub="Eventos del rango y filtros vigentes (cada línea structlog se persiste a log_events)"
        right={
          data.sinkDropped > 0 ? (
            <Badge variant="destructive" className="gap-1" title="Eventos perdidos por overflow de la cola del sink">
              <AlertTriangle className="size-3" />
              {formatInt(data.sinkDropped)} descartados por el sink
            </Badge>
          ) : undefined
        }
      />
      <PanelBody className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Kpi label="Eventos" value={formatInt(data.total)} />
          <Kpi label="Errores" value={formatInt(data.errors)} tone={data.errors > 0 ? "text-status-error" : undefined} />
          <Kpi
            label="Tasa de error"
            value={`${(data.errorRate * 100).toFixed(1)}%`}
            tone={data.errorRate > 0 ? "text-status-review" : undefined}
          />
          {hasLatency && <Kpi label="Latencia p95" value={fmtMs(lat.p95)} />}
        </div>

        {hist.length === 0 ? (
          <EmptyState title="Sin eventos en el rango" hint="Ampliá el rango o limpiá los filtros." />
        ) : (
          <HistChart data={hist} />
        )}

        {hasLatency && (
          <p className="num text-[11px] text-muted-foreground">
            Latencia (duration_ms): p50 {fmtMs(lat.p50)} · p95 {fmtMs(lat.p95)} · p99 {fmtMs(lat.p99)}
          </p>
        )}

        <div className="grid gap-3 lg:grid-cols-3">
          <CountList
            title="Por nivel"
            rows={data.byLevel.map((l) => ({
              key: l.level,
              label: l.level,
              count: l.count,
              cls: LEVEL_CLS[l.level],
            }))}
          />
          <CountList
            title="Top eventos"
            rows={data.byEvent.map((e) => ({ key: e.event, label: e.event, count: e.count }))}
          />
          <CountList
            title="Top loggers"
            rows={data.byLogger.map((g) => ({ key: g.logger, label: g.logger, count: g.count }))}
          />
        </div>
      </PanelBody>
    </Panel>
  )
}
