import { Sparkles } from "lucide-react"
import { StatusBadge } from "@/components/common/led"
import { formatCompact, formatDurationMs, formatUsd } from "@/lib/format"
import { llmTone } from "@/lib/status"
import { MODEL_PRICING, PURPOSE_LABEL } from "@/data"
import type { LlmExchange, LlmStatus } from "@/types/domain"

const STATUS_LABEL: Record<LlmStatus, string> = { ok: "OK", error: "Error", filtered: "Filtrado" }

export function LlmExchangeCard({ ex }: { ex: LlmExchange }) {
  return (
    <div className="mt-3 rounded-md border border-brand/25 bg-brand/[0.04]">
      <div className="flex flex-wrap items-center gap-2 border-b border-brand/15 px-3 py-2">
        <Sparkles className="size-3.5 text-brand" />
        <span className="eyebrow text-brand">llamada al LLM</span>
        <span className="text-xs font-medium">{PURPOSE_LABEL[ex.purpose]}</span>
        <span className="num text-xs text-muted-foreground">{MODEL_PRICING[ex.model]?.label ?? ex.model}</span>
        <span className="ml-auto">
          <StatusBadge tone={llmTone(ex.status)} label={STATUS_LABEL[ex.status]} />
        </span>
      </div>
      <div className="space-y-2.5 px-3 py-2.5">
        <div className="num flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          <span>tokens <span className="text-foreground">{formatCompact(ex.promptTokens)}↑ {formatCompact(ex.completionTokens)}↓</span></span>
          <span>costo <span className="text-foreground">{formatUsd(ex.costUsd)}</span></span>
          <span>latencia <span className="text-foreground">{formatDurationMs(ex.latencyMs)}</span></span>
        </div>
        <div>
          <div className="eyebrow mb-1">entrada</div>
          <p className="text-xs text-muted-foreground">{ex.inputSummary}</p>
        </div>
        <div>
          <div className="eyebrow mb-1">lo que devolvió el LLM</div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded border border-border bg-muted/30 p-2 font-mono text-[11px] text-foreground">
            {ex.output}
          </pre>
        </div>
      </div>
    </div>
  )
}
