import { useEffect, useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowDown, ArrowUp, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { EmptyState, ErrorState, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatCompact, formatDurationMs, formatUsd } from "@/lib/format"
import { llmTone } from "@/lib/status"
import { isBatchModule, moduleLabel } from "@/lib/metrics"
import {
  fetchLlmCalls,
  type FilterMode,
  type MetricsWindow,
  type ModelCost,
  type SourceCost,
} from "@/data"
import { useAsync } from "@/lib/use-async"
import type { LlmStatus } from "@/types/domain"

type SortKey = "created_at" | "cost_usd" | "latency_ms"
const PAGE = 12

const STATUS_LABEL: Record<string, string> = { ok: "OK", error: "Error", filtered: "Filtrado" }

/** Filtro de una dimensión: un valor a aislar/excluir (vacío = "all" → sin filtro). */
interface Dim {
  value: string
  mode: FilterMode
}
const ALL: Dim = { value: "all", mode: "include" }

function useDebouncedValue<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

function asList(d: Dim): string[] | undefined {
  return d.value !== "all" ? [d.value] : undefined
}

export function LlmAudit({
  window: win,
  modules,
  byModel,
  bySource,
}: {
  window: MetricsWindow
  modules: string[]
  byModel: ModelCost[]
  bySource: SourceCost[]
}) {
  const [statusF, setStatusF] = useState<Dim>(ALL)
  const [moduleF, setModuleF] = useState<Dim>(ALL)
  const [modelF, setModelF] = useState<Dim>(ALL)
  const [sourceF, setSourceF] = useState<Dim>(ALL)
  const [query, setQuery] = useState("")
  const debouncedQuery = useDebouncedValue(query.trim(), 300)
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "created_at", dir: "desc" })
  const [page, setPage] = useState(0)

  // Volver a la primera página cuando cambia cualquier filtro/orden/rango. Patrón "ajustar estado en
  // render" de React (no un efecto): React re-renderiza con page=0 ANTES de correr el fetch, sin
  // renders en cascada ni doble pedido.
  const filterKey = `${win.since ?? ""}|${win.until ?? ""}|${statusF.value}:${statusF.mode}|${moduleF.value}:${moduleF.mode}|${modelF.value}:${modelF.mode}|${sourceF.value}:${sourceF.mode}|${debouncedQuery}|${sort.key}:${sort.dir}`
  const [prevKey, setPrevKey] = useState(filterKey)
  if (filterKey !== prevKey) {
    setPrevKey(filterKey)
    setPage(0)
  }

  const { data, loading, error, reload } = useAsync(
    () =>
      fetchLlmCalls({
        ...win,
        status: asList(statusF),
        statusMode: statusF.mode,
        module: asList(moduleF),
        moduleMode: moduleF.mode,
        model: asList(modelF),
        modelMode: modelF.mode,
        source: asList(sourceF),
        sourceMode: sourceF.mode,
        q: debouncedQuery || undefined,
        sort: sort.key,
        dir: sort.dir,
        limit: PAGE,
        offset: page * PAGE,
      }),
    [
      win.since,
      win.until,
      win.tz,
      statusF.value,
      statusF.mode,
      moduleF.value,
      moduleF.mode,
      modelF.value,
      modelF.mode,
      sourceF.value,
      sourceF.mode,
      debouncedQuery,
      sort.key,
      sort.dir,
      page,
    ],
  )

  const rows = data?.items ?? []
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE))
  const untabulated = useMemo(
    () => new Set(byModel.filter((m) => m.untabulated).map((m) => m.model)),
    [byModel],
  )

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }))
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="Auditoría · llm_calls"
        title="Llamadas al LLM"
        sub={`${total} llamadas con los filtros actuales · ordená y saltá a la traza del mensaje`}
        right={
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="inbox / request / modelo / purpose"
              className="h-8 w-64 pl-7 text-xs"
            />
          </div>
        }
      />
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <DimFilter
          placeholder="Estado"
          dim={statusF}
          onChange={setStatusF}
          options={[
            { value: "all", label: "Todo estado" },
            { value: "ok", label: "OK" },
            { value: "error", label: "Error" },
            { value: "filtered", label: "Filtrado" },
          ]}
        />
        <DimFilter
          placeholder="Módulo"
          dim={moduleF}
          onChange={setModuleF}
          options={[{ value: "all", label: "Todo módulo" }, ...modules.map((m) => ({ value: m, label: moduleLabel(m) }))]}
        />
        <DimFilter
          placeholder="Modelo"
          dim={modelF}
          onChange={setModelF}
          options={[{ value: "all", label: "Todo modelo" }, ...byModel.map((m) => ({ value: m.model, label: m.model }))]}
        />
        <DimFilter
          placeholder="Fuente"
          dim={sourceF}
          onChange={setSourceF}
          options={[
            { value: "all", label: "Toda fuente" },
            ...bySource.map((s) => ({ value: s.sourceName, label: s.sourceName })),
          ]}
        />
      </div>
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <TableSkeleton rows={PAGE} cols={7} />
        ) : rows.length === 0 ? (
          <EmptyState title="Sin llamadas que coincidan" hint="Probá ampliar el rango o limpiar los filtros." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/30 text-left">
                  <SortHead k="created_at" sort={sort} onToggle={toggleSort}>Hora</SortHead>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Módulo</th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Modelo</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">Tokens (p/c)</th>
                  <SortHead k="cost_usd" sort={sort} onToggle={toggleSort} className="text-right">Costo</SortHead>
                  <SortHead k="latency_ms" sort={sort} onToggle={toggleSort} className="text-right">Latencia</SortHead>
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
                    <td className="px-3 py-2">
                      <div className="font-medium">{moduleLabel(c.module)}</div>
                      <div className="num text-[11px] text-muted-foreground">{c.purpose}</div>
                    </td>
                    <td className="num px-3 py-2 text-muted-foreground">{c.model}</td>
                    <td className="num px-3 py-2 text-right text-muted-foreground">
                      {formatCompact(c.promptTokens)}
                      <span className="opacity-50"> / </span>
                      {formatCompact(c.completionTokens)}
                    </td>
                    <td className="num px-3 py-2 text-right font-medium">
                      {c.status === "ok" ? (
                        untabulated.has(c.model) ? (
                          <span className="text-status-review" title="precio no tabulado (modelo sin tarifa)">
                            {formatUsd(0)}
                          </span>
                        ) : (
                          formatUsd(c.costUsd)
                        )
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td
                      className={cn(
                        "num px-3 py-2 text-right",
                        c.latencyMs > 8000 ? "text-status-review" : "text-muted-foreground",
                      )}
                    >
                      {c.latencyMs > 0 ? formatDurationMs(c.latencyMs) : "—"}
                    </td>
                    <td className="px-3 py-2">
                      <span title={c.errorMessage ?? undefined}>
                        <StatusBadge tone={llmTone(c.status as LlmStatus)} label={STATUS_LABEL[c.status] ?? c.status} />
                      </span>
                    </td>
                    <td className="num px-3 py-2 text-muted-foreground">
                      {c.inboxId !== null ? (
                        <Link to={`/datos/${c.inboxId}`} className="text-origin-inbox hover:underline">
                          #{c.inboxId}
                        </Link>
                      ) : isBatchModule(c.module) ? (
                        <span className="opacity-50" title="batch: la llamada cubre N mensajes (sin inbox_id)">
                          batch
                        </span>
                      ) : (
                        <span className="opacity-40" title="sin inbox asociado">
                          —
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelBody>
      <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
        <span className="num">
          {total === 0 ? 0 : page * PAGE + 1}–{Math.min(total, page * PAGE + PAGE)} de {total}
        </span>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-7" disabled={page === 0} onClick={() => setPage(page - 1)}>
            Anterior
          </Button>
          <span className="num">{page + 1}/{pageCount}</span>
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            disabled={page >= pageCount - 1}
            onClick={() => setPage(page + 1)}
          >
            Siguiente
          </Button>
        </div>
      </div>
    </Panel>
  )
}

function SortHead({
  k,
  sort,
  onToggle,
  children,
  className,
}: {
  k: SortKey
  sort: { key: SortKey; dir: "asc" | "desc" }
  onToggle: (k: SortKey) => void
  children: React.ReactNode
  className?: string
}) {
  const active = sort.key === k
  return (
    <th className={cn("px-3 py-2 font-medium text-muted-foreground", className)}>
      <button onClick={() => onToggle(k)} className="inline-flex items-center gap-1 hover:text-foreground">
        {children}
        {active && (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
      </button>
    </th>
  )
}

function DimFilter({
  placeholder,
  dim,
  onChange,
  options,
}: {
  placeholder: string
  dim: Dim
  onChange: (d: Dim) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div className="flex items-center gap-1">
      <Select value={dim.value} onValueChange={(v) => onChange({ value: v, mode: dim.mode })}>
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
      {dim.value !== "all" && (
        <div className="flex overflow-hidden rounded-md border border-border text-[10px] font-medium">
          <button
            onClick={() => onChange({ value: dim.value, mode: "include" })}
            className={cn("px-1.5 py-1.5", dim.mode === "include" ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:text-foreground")}
            title="Ver solo este valor"
          >
            solo
          </button>
          <button
            onClick={() => onChange({ value: dim.value, mode: "exclude" })}
            className={cn("px-1.5 py-1.5", dim.mode === "exclude" ? "bg-status-error/15 text-status-error" : "text-muted-foreground hover:text-foreground")}
            title="Excluir este valor"
          >
            excluir
          </button>
        </div>
      )}
    </div>
  )
}
