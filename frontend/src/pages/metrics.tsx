import { PageHeader } from "@/components/common/page-header"
import { CostBreakdown } from "@/components/features/metrics/cost-breakdown"
import { CostKpis } from "@/components/features/metrics/cost-kpis"
import { CostTrend } from "@/components/features/metrics/cost-trend"
import { LlmAudit } from "@/components/features/metrics/llm-audit"

export function MetricsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Vista · métricas"
        title="Métricas y costo LLM"
        description="El gasto del LLM de un vistazo: KPIs con variación, desglose por propósito y modelo, tendencia diaria y auditoría de cada llamada. El rango temporal (arriba) filtra toda la vista."
      />
      <CostKpis />
      <div className="grid gap-5 xl:grid-cols-2">
        <CostTrend />
        <CostBreakdown />
      </div>
      <LlmAudit />
    </div>
  )
}
