// Sección "Costos de ingesta (Apify)" de /metricas: lo que COBRÓ Apify por cada corrida de
// scraping social (tabla apify_runs), agregado server-side. A diferencia del costo LLM (calculado
// local con tabla de precios), acá el número viene del proveedor (usageTotalUsd por run).

import { Loader2 } from "lucide-react"
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { MeasuredBox } from "@/components/common/measured-box"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { formatInt, formatUsd, pctShare } from "@/lib/format"
import { useAsync } from "@/lib/use-async"
import {
  fetchApifyRollup,
  fetchApifyRuns,
  type ApifyAccountCost,
  type ApifySourceCost,
  type MetricsWindow,
} from "@/data"

const PLATFORM_COLOR: Record<string, string> = {
  x: "var(--chart-4)",
  instagram: "var(--chart-2)",
  facebook: "var(--chart-1)",
}

function platformColor(p: string): string {
  return PLATFORM_COLOR[p] ?? "var(--chart-6)"
}

const dayFmt = new Intl.DateTimeFormat("es-MX", { day: "2-digit", month: "short" })

function dayLabel(day: string): string {
  return dayFmt.format(new Date(`${day}T00:00:00`))
}

const tsFmt = new Intl.DateTimeFormat("es-MX", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
})

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
              {String(p.dataKey)}
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

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <div className="num text-lg font-semibold">{value}</div>
      <div className="eyebrow mt-0.5">{label}</div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

/** Barras horizontales de gasto (mismo lenguaje visual que CostBySource). */
function CostBars({
  rows,
  empty,
}: {
  rows: { key: string; label: string; sub: string; costUsd: number; color: string; muted?: boolean }[]
  empty: string
}) {
  if (rows.length === 0) return <EmptyState title={empty} />
  const max = Math.max(...rows.map((r) => r.costUsd), 0.0001)
  const total = rows.reduce((a, r) => a + r.costUsd, 0)
  return (
    <div className="space-y-3">
      {rows.map((r) => (
        <div key={r.key}>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="flex items-center gap-2">
              <span className="size-2 rounded-[2px]" style={{ background: r.color }} />
              <span className={r.muted ? "font-medium italic text-muted-foreground" : "font-medium"}>
                {r.label}
              </span>
              <span className="num text-muted-foreground">{r.sub}</span>
            </span>
            <span className="num font-medium">
              {formatUsd(r.costUsd)}
              <span className="ml-1.5 text-muted-foreground">{pctShare(r.costUsd, total)}</span>
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full"
              style={{ width: `${(r.costUsd / max) * 100}%`, background: r.color }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function sourceBars(bySource: ApifySourceCost[]) {
  return bySource.map((s) => ({
    key: `${s.sourceId ?? "x"}-${s.sourceName}`,
    label: s.sourceName,
    sub: `${formatInt(s.runs)} runs · ${formatInt(s.itemsScraped)} posts`,
    costUsd: s.costUsd,
    color: "var(--chart-3)",
    muted: s.sourceId === null,
  }))
}

function accountBars(byAccount: ApifyAccountCost[]) {
  return byAccount.map((a) => ({
    key: `${a.platform}:${a.account}`,
    label: `@${a.account}`,
    sub: `${a.platform} · ${formatInt(a.runs)} runs · ${formatInt(a.itemsScraped)} posts`,
    costUsd: a.costUsd,
    color: platformColor(a.platform),
  }))
}

const RUN_TONE: Record<string, "ok" | "error" | "review"> = {
  ok: "ok",
  error: "error",
  timeout: "review",
}

/** Auditoría compacta: los runs de actor más recientes del rango (el endpoint pagina/filtra más). */
function RecentRuns({ window: win }: { window: MetricsWindow }) {
  const { data } = useAsync(
    () => fetchApifyRuns(win, { limit: 12 }),
    [win.since, win.until, win.tz],
  )
  if (!data || data.items.length === 0) return null
  return (
    <div>
      <div className="eyebrow mb-2">últimos runs de actor</div>
      <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
        {data.items.map((r) => (
          <div key={r.id} className="flex items-center gap-2.5 px-3 py-1.5 text-xs">
            <span className="num shrink-0 text-muted-foreground">
              {tsFmt.format(new Date(r.createdAt))}
            </span>
            <span className="size-2 shrink-0 rounded-[2px]" style={{ background: platformColor(r.platform) }} />
            <span className="num min-w-0 truncate font-medium">@{r.account}</span>
            <StatusBadge tone={RUN_TONE[r.status] ?? "neutral"} label={r.status} />
            <span className="num text-muted-foreground">
              {formatInt(r.itemsScraped)} scrapeados · {formatInt(r.itemsKept)} nuevos
            </span>
            <span className="num ml-auto shrink-0 font-medium">
              {r.costUsd !== null ? formatUsd(r.costUsd) : "(sin costo aún)"}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-1.5 text-[11px] text-muted-foreground">
        {formatInt(data.total)} runs en el rango. Un run con error/timeout también puede haber
        cobrado lo consumido; "(sin costo aún)" = Apify no lo asentó al momento de registrar.
      </p>
    </div>
  )
}

/** Gasto Apify del rango: KPIs, tendencia por plataforma, por fuente y por cuenta seguida. */
export function ApifyCosts({ window: win }: { window: MetricsWindow }) {
  const { data, loading, error, reload } = useAsync(
    () => fetchApifyRollup(win),
    [win.since, win.until, win.tz],
  )

  const k = data?.kpis
  const deltaHint = (() => {
    if (!k || k.prevCostUsd === null) return undefined
    if (k.prevCostUsd === 0) return k.costUsd > 0 ? "periodo previo sin gasto" : undefined
    const pct = ((k.costUsd - k.prevCostUsd) / k.prevCostUsd) * 100
    return `${pct >= 0 ? "+" : ""}${pct.toFixed(0)}% vs periodo previo`
  })()

  const trend = (data?.daily ?? []).map((d) => {
    const row: Record<string, number | string> = { label: dayLabel(d.day) }
    for (const p of data?.platforms ?? []) row[p] = d.byPlatform[p] ?? 0
    return row
  })

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · apify"
        title="Costos de ingesta (Apify)"
        sub="Lo que cobró Apify por cada corrida de scraping social (apify_runs) — un run de actor por cuenta seguida; incluye dry-runs y corridas fallidas, que gastan igual"
      />
      <PanelBody className="space-y-4">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando gasto Apify…
          </div>
        ) : !data || data.kpis.runs === 0 ? (
          <EmptyState
            title="Sin corridas de Apify en el rango"
            hint="Traé de una red social en /carga (o esperá al daemon) y el gasto aparece acá."
          />
        ) : (
          <>
            <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
              <Kpi label="gasto" value={formatUsd(data.kpis.costUsd)} hint={deltaHint} />
              <Kpi label="runs de actor" value={formatInt(data.kpis.runs)} />
              <Kpi label="posts scrapeados" value={formatInt(data.kpis.itemsScraped)} />
              <Kpi label="posts nuevos" value={formatInt(data.kpis.itemsKept)} />
              <Kpi label="runs con error" value={formatInt(data.kpis.errors)} />
              <Kpi label="cuentas activas" value={formatInt(data.kpis.accounts)} />
            </div>

            {trend.length > 1 && (
              <MeasuredBox className="h-44 w-full">
                {({ w, h }) => (
                  <AreaChart width={w} height={h} data={trend} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
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
                      width={52}
                      tickFormatter={(v) => `$${Number(v).toFixed(2)}`}
                    />
                    <Tooltip content={<TrendTooltip />} />
                    {(data.platforms ?? []).map((p) => (
                      <Area
                        key={p}
                        type="monotone"
                        dataKey={p}
                        stackId="1"
                        stroke={platformColor(p)}
                        fill={platformColor(p)}
                        fillOpacity={0.18}
                        strokeWidth={1.5}
                      />
                    ))}
                  </AreaChart>
                )}
              </MeasuredBox>
            )}

            <div className="grid gap-5 xl:grid-cols-2">
              <div>
                <div className="eyebrow mb-2">por cuenta seguida</div>
                <CostBars rows={accountBars(data.byAccount)} empty="Sin gasto por cuenta" />
              </div>
              <div>
                <div className="eyebrow mb-2">por fuente</div>
                <CostBars rows={sourceBars(data.bySource)} empty="Sin gasto por fuente" />
              </div>
            </div>

            <RecentRuns window={win} />
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
