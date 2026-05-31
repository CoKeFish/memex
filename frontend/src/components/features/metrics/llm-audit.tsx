import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatCompact, formatDurationMs, formatPct, formatUsd } from "@/lib/format"
import { llmTone } from "@/lib/status"
import { callsInRange } from "@/lib/selectors"
import { MODEL_PRICING, PURPOSES, PURPOSE_LABEL } from "@/mocks/catalog"
import { useTimeRange } from "@/state/time-range"
import type { LlmCall } from "@/types/domain"

type SortKey = "createdAt" | "costUsd" | "latencyMs"
const PAGE = 12

const STATUS_LABEL: Record<LlmCall["status"], string> = { ok: "OK", error: "Error", filtered: "Filtrado" }

export function LlmAudit() {
  const { range } = useTimeRange()
  const all = callsInRange(range)

  const [purpose, setPurpose] = useState<string>("all")
  const [status, setStatus] = useState<string>("all")
  const [model, setModel] = useState<string>("all")
  const [query, setQuery] = useState("")
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "createdAt", dir: "desc" })
  const [page, setPage] = useState(0)

  const models = useMemo(() => [...new Set(all.map((c) => c.model))], [all])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const rows = all.filter((c) => {
      if (purpose !== "all" && c.purpose !== purpose) return false
      if (status !== "all" && c.status !== status) return false
      if (model !== "all" && c.model !== model) return false
      if (q && !`${c.inboxId ?? ""} ${c.requestId} ${c.model}`.toLowerCase().includes(q)) return false
      return true
    })
    rows.sort((a, b) => {
      const av = sort.key === "createdAt" ? new Date(a.createdAt).getTime() : a[sort.key]
      const bv = sort.key === "createdAt" ? new Date(b.createdAt).getTime() : b[sort.key]
      return sort.dir === "asc" ? av - bv : bv - av
    })
    return rows
  }, [all, purpose, status, model, query, sort])

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE))
  const safePage = Math.min(page, pageCount - 1)
  const rows = filtered.slice(safePage * PAGE, safePage * PAGE + PAGE)
  const cacheHitRatio = useMemo(() => {
    const prompt = filtered.reduce((a, c) => a + c.promptTokens, 0)
    const hit = filtered.reduce((a, c) => a + c.cacheHitTokens, 0)
    return prompt ? hit / prompt : 0
  }, [filtered])

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }))
    setPage(0)
  }

  function SortHead({ k, children, className }: { k: SortKey; children: React.ReactNode; className?: string }) {
    const active = sort.key === k
    return (
      <th className={cn("px-3 py-2 font-medium text-muted-foreground", className)}>
        <button onClick={() => toggleSort(k)} className="inline-flex items-center gap-1 hover:text-foreground">
          {children}
          {active &&
            (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
        </button>
      </th>
    )
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="Auditoría · llm_calls"
        title="Llamadas al LLM"
        sub={`${filtered.length} llamadas · cache-hit ${formatPct(cacheHitRatio, 0)} de los prompt tokens`}
        right={
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                setPage(0)
              }}
              placeholder="inbox / request / modelo"
              className="h-8 w-56 pl-7 text-xs"
            />
          </div>
        }
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-2.5">
        <FilterSelect value={purpose} onChange={(v) => { setPurpose(v); setPage(0) }} placeholder="Propósito"
          options={[{ value: "all", label: "Todo propósito" }, ...PURPOSES.map((p) => ({ value: p.key, label: p.label }))]} />
        <FilterSelect value={model} onChange={(v) => { setModel(v); setPage(0) }} placeholder="Modelo"
          options={[{ value: "all", label: "Todo modelo" }, ...models.map((m) => ({ value: m, label: MODEL_PRICING[m]?.label ?? m }))]} />
        <FilterSelect value={status} onChange={(v) => { setStatus(v); setPage(0) }} placeholder="Estado"
          options={[{ value: "all", label: "Todo estado" }, { value: "ok", label: "OK" }, { value: "error", label: "Error" }, { value: "filtered", label: "Filtrado" }]} />
      </div>
      <PanelBody className="p-0">
        <Stateful
          skeleton={<TableSkeleton rows={PAGE} cols={7} />}
          empty={<EmptyState title="Sin llamadas que coincidan" hint="Probá ampliar el rango o limpiar los filtros." />}
          errorDetail="HTTP 500 — GET /metrics/llm-calls falló"
        >
          {rows.length === 0 ? (
            <EmptyState title="Sin llamadas que coincidan" hint="Ajustá los filtros." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border bg-muted/30 text-left">
                    <SortHead k="createdAt">Hora</SortHead>
                    <th className="px-3 py-2 font-medium text-muted-foreground">Propósito</th>
                    <th className="px-3 py-2 font-medium text-muted-foreground">Modelo</th>
                    <th className="px-3 py-2 text-right font-medium text-muted-foreground">Tokens (p/c)</th>
                    <SortHead k="costUsd" className="text-right">Costo</SortHead>
                    <SortHead k="latencyMs" className="text-right">Latencia</SortHead>
                    <th className="px-3 py-2 font-medium text-muted-foreground">Estado</th>
                    <th className="px-3 py-2 font-medium text-muted-foreground">inbox</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((c) => (
                    <tr key={c.id} className="align-top hover:bg-accent/30">
                      <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                        <RelativeTime date={c.createdAt} />
                      </td>
                      <td className="px-3 py-2">{PURPOSE_LABEL[c.purpose]}</td>
                      <td className="num px-3 py-2 text-muted-foreground">{MODEL_PRICING[c.model]?.label ?? c.model}</td>
                      <td className="num px-3 py-2 text-right text-muted-foreground">
                        {formatCompact(c.promptTokens)}
                        <span className="opacity-50"> / </span>
                        {formatCompact(c.completionTokens)}
                      </td>
                      <td className="num px-3 py-2 text-right font-medium">
                        {c.status === "ok" ? (
                          MODEL_PRICING[c.model]?.untabulated ? (
                            <span className="text-status-review" title="precio no resuelto (modelo no tabulado)">{formatUsd(0)}</span>
                          ) : (
                            formatUsd(c.costUsd)
                          )
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className={cn("num px-3 py-2 text-right", c.latencyMs > 4000 ? "text-status-review" : "text-muted-foreground")}>
                        {formatDurationMs(c.latencyMs)}
                      </td>
                      <td className="px-3 py-2">
                        <span title={c.errorMessage ?? undefined}>
                          <StatusBadge tone={llmTone(c.status)} label={STATUS_LABEL[c.status]} />
                        </span>
                      </td>
                      <td className="num px-3 py-2 text-muted-foreground">
                        {c.inboxId !== null ? (
                          <span className="text-origin-inbox">#{c.inboxId}</span>
                        ) : (
                          <span className="opacity-50" title="batch: la llamada cubre N mensajes (sin inbox_id)">batch</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Stateful>
      </PanelBody>
      <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
        <span className="num">
          {filtered.length === 0 ? 0 : safePage * PAGE + 1}–{Math.min(filtered.length, safePage * PAGE + PAGE)} de {filtered.length}
        </span>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-7" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
            Anterior
          </Button>
          <span className="num">{safePage + 1}/{pageCount}</span>
          <Button variant="outline" size="sm" className="h-7" disabled={safePage >= pageCount - 1} onClick={() => setPage(safePage + 1)}>
            Siguiente
          </Button>
        </div>
      </div>
    </Panel>
  )
}

function FilterSelect({
  value,
  onChange,
  placeholder,
  options,
}: {
  value: string
  onChange: (v: string) => void
  placeholder: string
  options: { value: string; label: string }[]
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="h-8 w-auto min-w-[130px] text-xs" aria-label={placeholder}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value} className="text-xs">
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
