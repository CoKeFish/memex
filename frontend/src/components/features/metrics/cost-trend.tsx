import { useLayoutEffect, useRef, useState } from "react"
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts"
import { EmptyState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatUsd } from "@/lib/format"
import { moduleChart, moduleLabel } from "@/lib/metrics"
import type { DailyCost } from "@/data"

const dayFmt = new Intl.DateTimeFormat("es-MX", { day: "2-digit", month: "short" })

/** 'YYYY-MM-DD' → "02 jun" parseando como hora local (evita el corrimiento de día por UTC). */
function dayLabel(day: string): string {
  return dayFmt.format(new Date(`${day}T00:00:00`))
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function TrendTooltip({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) {
  if (!active || !payload?.length) return null
  const total = payload.reduce((a, p) => a + (p.value ?? 0), 0)
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="eyebrow mb-1.5">{label}</div>
      {payload
        .filter((p) => p.value > 0)
        .map((p) => (
          <div key={p.dataKey} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5">
              <span className="size-2 rounded-[2px]" style={{ background: p.color }} />
              {moduleLabel(String(p.dataKey))}
            </span>
            <span className="num">{formatUsd(p.value)}</span>
          </div>
        ))}
      <div className="mt-1.5 flex justify-between gap-4 border-t border-border pt-1.5 font-medium">
        <span>Total</span>
        <span className="num">{formatUsd(total)}</span>
      </div>
    </div>
  )
}

type TrendRow = Record<string, number | string>

/** Área apilada con dimensiones MEDIDAS (sin `ResponsiveContainer`): el chart siempre recibe un
 *  tamaño positivo, así que nunca dispara el warning "width(-1)/height(-1)" de Recharts (que
 *  `ResponsiveContainer` loguea en su primer render con tamaño -1, y StrictMode amplifica al
 *  re-montar). `useLayoutEffect` mide antes del paint (sin parpadeo) y un ResizeObserver lo mantiene
 *  responsivo. Se monta solo cuando hay datos, así su efecto corre con el contenedor ya en layout. */
function StackedArea({ data, modules }: { data: TrendRow[]; modules: string[] }) {
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
    <div ref={ref} className="h-64 w-full">
      {size && (
        <AreaChart
          width={size.w}
          height={size.h}
          data={data}
          margin={{ top: 4, right: 8, left: -12, bottom: 0 }}
        >
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
            width={48}
            tickFormatter={(v) => `$${Number(v).toFixed(2)}`}
          />
          <Tooltip content={<TrendTooltip />} />
          {modules.map((m) => (
            <Area
              key={m}
              type="monotone"
              dataKey={m}
              stackId="1"
              stroke={moduleChart(m)}
              fill={moduleChart(m)}
              fillOpacity={0.18}
              strokeWidth={1.5}
            />
          ))}
        </AreaChart>
      )}
    </div>
  )
}

export function CostTrend({ daily, modules, tz }: { daily: DailyCost[]; modules: string[]; tz?: string }) {
  // Serie ancha para Recharts: una columna por módulo presente (rellena 0 donde no hubo gasto).
  const data: TrendRow[] = daily.map((d) => {
    const row: TrendRow = { label: dayLabel(d.day) }
    for (const m of modules) row[m] = d.byModule[m] ?? 0
    return row
  })

  return (
    <Panel>
      <PanelHeader
        eyebrow="Tendencia · costo diario"
        title="Gasto LLM en el tiempo"
        sub={`Área apilada por módulo (llm_calls.cost_usd, zona ${tz ?? "local"})`}
      />
      <PanelBody>
        {data.length === 0 ? (
          <EmptyState title="Sin gasto en el rango" />
        ) : (
          <StackedArea data={data} modules={modules} />
        )}
      </PanelBody>
    </Panel>
  )
}
