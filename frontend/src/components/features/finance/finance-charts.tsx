import { Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatMoney, formatPct } from "@/lib/format"
import { CATEGORIES, CATEGORY_LABEL, financeByCategory, financeByMerchant, financeByMonth } from "@/data"
import type { ExpenseCategory } from "@/types/domain"

function axisMoney(v: number): string {
  return Math.abs(v) >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function MoneyTooltip({ active, payload, label, currency }: { active?: boolean; payload?: any[]; label?: string; currency: string }) {
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
              {CATEGORY_LABEL[p.dataKey as ExpenseCategory]}
            </span>
            <span className="num">{formatMoney(p.value, currency)}</span>
          </div>
        ))}
      <div className="mt-1.5 flex justify-between gap-4 border-t border-border pt-1.5 font-medium">
        <span>Total</span>
        <span className="num">{formatMoney(total, currency)}</span>
      </div>
    </div>
  )
}

export function MonthlyTrend({ currency }: { currency: string }) {
  const data = financeByMonth(currency).map((p) => ({ label: p.label, ...p.byCategory }))
  return (
    <Panel>
      <PanelHeader eyebrow="finanzas · tendencia" title="Gasto mensual" sub={`Apilado por tipo de gasto · ${currency}`} />
      <PanelBody>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 4, right: 8, left: -8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} tickLine={false} axisLine={false} />
              <YAxis tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} tickLine={false} axisLine={false} width={42} tickFormatter={axisMoney} />
              <Tooltip cursor={{ fill: "var(--muted)", opacity: 0.3 }} content={<MoneyTooltip currency={currency} />} />
              {CATEGORIES.map((c) => (
                <Bar key={c.key} dataKey={c.key} stackId="1" fill={c.chart} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      </PanelBody>
    </Panel>
  )
}

export function CategoryBreakdown({ currency }: { currency: string }) {
  const rows = financeByCategory(currency)
  const total = rows.reduce((a, r) => a + r.total, 0) || 1
  return (
    <Panel>
      <PanelHeader eyebrow="finanzas · categorías" title="Por tipo de gasto" sub={`Todo el periodo · ${currency}`} />
      <PanelBody>
        <div className="grid items-center gap-4 sm:grid-cols-[170px_1fr]">
          <div className="h-44">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={rows} dataKey="total" nameKey="label" innerRadius={48} outerRadius={72} paddingAngle={2} stroke="none">
                  {rows.map((r) => (
                    <Cell key={r.category} fill={r.chart} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
          </div>
          <ul className="space-y-1.5">
            {rows.map((r) => (
              <li key={r.category} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex items-center gap-2">
                  <span className="size-2.5 rounded-[3px]" style={{ background: r.chart }} />
                  {r.label}
                  <span className="num text-xs text-muted-foreground">{r.count}</span>
                </span>
                <span className="num font-medium">
                  {formatMoney(r.total, currency)}
                  <span className="ml-1.5 text-muted-foreground">{formatPct(r.total / total, 0)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </PanelBody>
    </Panel>
  )
}

export function TopMerchants({ currency }: { currency: string }) {
  const rows = financeByMerchant(currency).slice(0, 8)
  const max = Math.max(...rows.map((r) => r.total), 1)
  return (
    <Panel>
      <PanelHeader eyebrow="finanzas · comercios" title="Top comercios" sub={`Por gasto total · ${currency}`} />
      <PanelBody className="space-y-2.5">
        {rows.map((r) => (
          <div key={r.merchant}>
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="font-medium">
                {r.merchant} <span className="num text-muted-foreground">· {r.count}</span>
              </span>
              <span className="num font-medium">{formatMoney(r.total, currency)}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-brand" style={{ width: `${(r.total / max) * 100}%` }} />
            </div>
          </div>
        ))}
      </PanelBody>
    </Panel>
  )
}
