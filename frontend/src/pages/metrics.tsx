import { useState } from "react"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { MetricsFilters } from "@/components/features/metrics/metrics-filters"
import { CostKpis } from "@/components/features/metrics/cost-kpis"
import { CostTrend } from "@/components/features/metrics/cost-trend"
import { CostBreakdown } from "@/components/features/metrics/cost-breakdown"
import { CostBySource } from "@/components/features/metrics/cost-by-source"
import { SourceModuleMatrix } from "@/components/features/metrics/source-module-matrix"
import { Outliers } from "@/components/features/metrics/outliers"
import { RecentErrors } from "@/components/features/metrics/recent-errors"
import { LlmAudit } from "@/components/features/metrics/llm-audit"
import { fetchLlmRollup, presetWindow, type MetricsWindow } from "@/data"
import { activeDisplayTz } from "@/lib/timezone"
import { useAsync } from "@/lib/use-async"
import { MetricsTzProvider } from "@/state/metrics-tz"

export function MetricsPage() {
  // Rango LOCAL de la vista (ver MetricsFilters) — alimenta el rollup y la auditoría. La TZ activa
  // (autodetectada/override) ancla "hoy" y los días; `win.tz` en las deps refetchea al cambiarla.
  const [win, setWin] = useState<MetricsWindow>(() => presetWindow("30d", activeDisplayTz()))
  const { data, loading, error, reload } = useAsync(
    () => fetchLlmRollup(win),
    [win.since, win.until, win.tz],
  )

  return (
    <MetricsTzProvider>
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · métricas"
        title="Métricas y costo LLM"
        description="El gasto del LLM contra datos reales (llm_calls): KPIs con variación, gasto por módulo y por fuente, matriz cruzada, tendencia diaria, outliers de costo/latencia, errores y auditoría de cada llamada con búsqueda y deep-link a la traza. El rango (arriba) filtra toda la vista."
        actions={<MetricsFilters onChange={setWin} />}
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando métricas…
        </div>
      ) : !data || data.kpis.calls === 0 ? (
        <EmptyState
          title="Sin llamadas LLM en el rango"
          hint="Ningún worker corrió en esta ventana. Probá ampliar el rango (arriba)."
        />
      ) : (
        <>
          <CostKpis kpis={data.kpis} daily={data.daily} />
          <div className="grid gap-5 xl:grid-cols-2">
            <CostTrend daily={data.daily} modules={data.modules} tz={win.tz} />
            <CostBreakdown byModule={data.byModule} byModel={data.byModel} />
          </div>
          <div className="grid gap-5 xl:grid-cols-[1fr_1.6fr]">
            <CostBySource bySource={data.bySource} />
            <SourceModuleMatrix bySourceModule={data.bySourceModule} modules={data.modules} />
          </div>
          <Outliers window={win} />
          <RecentErrors window={win} />
          <LlmAudit window={win} modules={data.modules} byModel={data.byModel} bySource={data.bySource} />
        </>
      )}
    </div>
    </MetricsTzProvider>
  )
}
