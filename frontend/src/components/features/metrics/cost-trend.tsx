import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { EmptyState, Stateful } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Skeleton } from "@/components/ui/skeleton"
import { formatUsd } from "@/lib/format"
import { costDaily, PURPOSE_LABEL, PURPOSES } from "@/data"
import { useTimeRange } from "@/state/time-range"
import type { LlmPurpose } from "@/types/domain"

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
              {PURPOSE_LABEL[p.dataKey as LlmPurpose]}
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

export function CostTrend() {
  const { range } = useTimeRange()
  const daily = costDaily(range)
  const data = daily.map((d) => ({ label: d.label, ...d.byPurpose }))

  return (
    <Panel>
      <PanelHeader
        eyebrow="Tendencia · costo diario"
        title="Gasto LLM en el tiempo"
        sub="Área apilada por propósito de la llamada (llm_calls.cost_usd)"
      />
      <PanelBody>
        <Stateful
          skeleton={<Skeleton className="h-64 w-full" />}
          empty={<EmptyState title="Sin gasto en el rango" />}
        >
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
                {PURPOSES.map((p) => (
                  <Area
                    key={p.key}
                    type="monotone"
                    dataKey={p.key}
                    stackId="1"
                    stroke={p.chart}
                    fill={p.chart}
                    fillOpacity={0.18}
                    strokeWidth={1.5}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Stateful>
      </PanelBody>
    </Panel>
  )
}
