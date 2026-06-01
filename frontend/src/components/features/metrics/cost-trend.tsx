import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
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

export function CostTrend({ daily, modules }: { daily: DailyCost[]; modules: string[] }) {
  // Serie ancha para Recharts: una columna por módulo presente (rellena 0 donde no hubo gasto).
  const data = daily.map((d) => {
    const row: Record<string, number | string> = { label: dayLabel(d.day) }
    for (const m of modules) row[m] = d.byModule[m] ?? 0
    return row
  })

  return (
    <Panel>
      <PanelHeader
        eyebrow="Tendencia · costo diario"
        title="Gasto LLM en el tiempo"
        sub="Área apilada por módulo (llm_calls.cost_usd, zona America/Mexico_City)"
      />
      <PanelBody>
        {data.length === 0 ? (
          <EmptyState title="Sin gasto en el rango" />
        ) : (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
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
            </ResponsiveContainer>
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
