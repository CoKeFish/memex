import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Panel } from "./panel"
import { Sparkline } from "./stat"

export function KpiCard({
  eyebrow,
  value,
  delta,
  sparkData,
  sparkStroke,
  footer,
  accent = false,
}: {
  eyebrow: string
  value: ReactNode
  delta?: ReactNode
  sparkData?: number[]
  sparkStroke?: string
  footer?: ReactNode
  accent?: boolean
}) {
  return (
    <Panel className="p-4">
      <div className="eyebrow">{eyebrow}</div>
      <div className="mt-2 flex items-end justify-between gap-2">
        <span className={cn("kpi text-2xl leading-none", accent && "text-brand")}>{value}</span>
        {delta}
      </div>
      {sparkData && sparkData.length > 1 && (
        <div className="mt-3 opacity-80">
          <Sparkline data={sparkData} stroke={sparkStroke} />
        </div>
      )}
      {footer && <div className="num mt-2 text-xs text-muted-foreground">{footer}</div>}
    </Panel>
  )
}
