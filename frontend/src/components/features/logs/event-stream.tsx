import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { Radio, Search, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { MetricsFilters } from "@/components/features/metrics/metrics-filters"
import {
  fetchLogStats,
  fetchLogs,
  presetWindow,
  type FilterMode,
  type LogsQuery,
  type MetricsWindow,
} from "@/data"
import { activeDisplayTz } from "@/lib/timezone"
import { useAsync } from "@/lib/use-async"
import { LogMetrics } from "./log-metrics"
import { LogRow } from "./log-row"
import type { LogLevel } from "@/types/domain"

const PAGE = 50
const LEVELS: LogLevel[] = ["debug", "info", "warning", "error", "critical"]
const LEVEL_LABEL: Record<LogLevel, string> = {
  debug: "Debug",
  info: "Info",
  warning: "Warning",
  error: "Error",
  critical: "Critical",
}

/** Filtro de una dimensión: un valor a aislar/excluir (vacío = "all" → sin filtro). Mismo patrón que
 *  la auditoría de llm_calls (llm-audit.tsx). */
interface Dim {
  value: string
  mode: FilterMode
}
const ALL: Dim = { value: "all", mode: "include" }

function asList(d: Dim): string[] | undefined {
  return d.value !== "all" ? [d.value] : undefined
}

function useDebouncedValue<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

/**
 * Stream de eventos contra `log_events` (sink real de structlog). Filtros SERVER-SIDE: rango+TZ
 * (MetricsFilters), búsqueda `q` (debounced), nivel/logger/event con incluir/excluir (DimFilter),
 * y un filtro por request_id (al clickear el chip "req" de una fila → la traza del mensaje). Pager
 * por offset con el total real (sin cap silencioso). El toggle "Tail en vivo" fuerza la primera
 * página descendente y deja que el tick de auto-refresco re-traiga lo nuevo al frente.
 */
export function EventStream() {
  // Ventana local con TZ por defecto activa (Bogota/override) — corrige la zona del stream viejo.
  const [win, setWin] = useState<MetricsWindow>(() => presetWindow("30d", activeDisplayTz()))
  const [query, setQuery] = useState("")
  const debouncedQuery = useDebouncedValue(query.trim(), 300)
  const [levelF, setLevelF] = useState<Dim>(ALL)
  const [loggerF, setLoggerF] = useState<Dim>(ALL)
  const [eventF, setEventF] = useState<Dim>(ALL)
  const [requestId, setRequestId] = useState<string | null>(null)
  // Deep-link desde /carga ("Corridas de ingesta" → logs?run_id=…): aísla la traza de una corrida.
  const [searchParams] = useSearchParams()
  const [runId, setRunId] = useState<string | null>(() => searchParams.get("run_id"))
  const [live, setLive] = useState(false)
  const [page, setPage] = useState(0)

  // En vivo: primera página, más nuevos primero; el pager se deshabilita y el tick de auto-refresh
  // (`now` de useAsync) re-trae el frente. Apagado: orden/paginación normales.
  const sort = "ts" as const
  const dir = "desc" as const
  const effectivePage = live ? 0 : page

  // Filtros sin sort/dir/limit/offset → alimentan tanto el stream como LogMetrics (mismo recorte).
  const baseFilters: LogsQuery = {
    ...win,
    level: asList(levelF),
    levelMode: levelF.mode,
    logger: asList(loggerF),
    loggerMode: loggerF.mode,
    event: asList(eventF),
    eventMode: eventF.mode,
    requestId: requestId ?? undefined,
    runId: runId ?? undefined,
    q: debouncedQuery || undefined,
  }

  // Opciones de logger/event: del corte agregado del rango+filtros (byLogger/byEvent de /logs/stats).
  const { data: stats } = useAsync(
    () => fetchLogStats(baseFilters),
    [
      win.tz,
      win.since,
      win.until,
      levelF.value,
      levelF.mode,
      loggerF.value,
      loggerF.mode,
      eventF.value,
      eventF.mode,
      requestId,
      runId,
      debouncedQuery,
    ],
  )

  // Volver a la primera página al cambiar cualquier filtro (patrón "ajustar estado en render").
  const filterKey = `${win.since ?? ""}|${win.until ?? ""}|${win.tz ?? ""}|${levelF.value}:${levelF.mode}|${loggerF.value}:${loggerF.mode}|${eventF.value}:${eventF.mode}|${requestId ?? ""}|${runId ?? ""}|${debouncedQuery}`
  const [prevKey, setPrevKey] = useState(filterKey)
  if (filterKey !== prevKey) {
    setPrevKey(filterKey)
    setPage(0)
  }

  const { data, loading, error, reload } = useAsync(
    () => fetchLogs({ ...baseFilters, sort, dir, limit: PAGE, offset: effectivePage * PAGE }),
    [
      win.tz,
      win.since,
      win.until,
      levelF.value,
      levelF.mode,
      loggerF.value,
      loggerF.mode,
      eventF.value,
      eventF.mode,
      requestId,
      runId,
      debouncedQuery,
      effectivePage,
      live,
    ],
  )

  const rows = data?.items ?? []
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE))

  function onFilterRequest(id: string): void {
    setRequestId(id)
    setLive(false)
  }

  const loggerOptions = [
    { value: "all", label: "Todo logger" },
    ...(stats?.byLogger ?? []).map((g) => ({ value: g.logger, label: g.logger })),
  ]
  const eventOptions = [
    { value: "all", label: "Todo evento" },
    ...(stats?.byEvent ?? []).map((e) => ({ value: e.event, label: e.event })),
  ]

  return (
    <div className="space-y-4">
      {/* Control de rango + TZ (reusa MetricsFilters; arregla la zona horaria → Bogota). */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <MetricsFilters onChange={setWin} />
        <Button
          variant={live ? "default" : "outline"}
          size="sm"
          onClick={() => setLive((v) => !v)}
          className="gap-1.5"
          title="Trae la primera página descendente y la refresca con el tick de auto-refresco"
        >
          <Radio className={cn("size-3.5", live && "animate-pulse")} />
          {live ? "En vivo" : "Tail en vivo"}
        </Button>
      </div>

      {/* Métricas del recorte vigente, ARRIBA del stream. */}
      <LogMetrics query={baseFilters} />

      <Panel className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-2">
          <DimFilter
            placeholder="Nivel"
            dim={levelF}
            onChange={setLevelF}
            options={[{ value: "all", label: "Todo nivel" }, ...LEVELS.map((l) => ({ value: l, label: LEVEL_LABEL[l] }))]}
          />
          <DimFilter placeholder="Logger" dim={loggerF} onChange={setLoggerF} options={loggerOptions} />
          <DimFilter placeholder="Evento" dim={eventF} onChange={setEventF} options={eventOptions} />
          {requestId && (
            <button
              onClick={() => setRequestId(null)}
              className="inline-flex items-center gap-1 rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[11px] font-medium text-brand hover:bg-brand/20"
              title="Quitar el filtro por request_id"
            >
              req {requestId.slice(0, 8)}
              <X className="size-3" />
            </button>
          )}
          {runId && (
            <button
              onClick={() => setRunId(null)}
              className="inline-flex items-center gap-1 rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[11px] font-medium text-brand hover:bg-brand/20"
              title="Quitar el filtro por run_id (corrida de ingesta)"
            >
              corrida {runId.slice(0, 8)}
              <X className="size-3" />
            </button>
          )}
          <div className="relative ml-auto">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="buscar en eventos"
              className="h-8 w-56 pl-7 text-xs"
            />
          </div>
        </div>

        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="Sin eventos" hint="No hay eventos para este rango y estos filtros." />
        ) : (
          <div className="max-h-[600px] overflow-y-auto">
            {rows.map((e) => (
              <LogRow key={e.id} event={e} onFilterRequest={onFilterRequest} />
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
          {live ? (
            <span className="inline-flex items-center gap-1.5 text-status-ok">
              <Radio className="size-3 animate-pulse" /> en vivo · {total} eventos · primera página al frente
            </span>
          ) : (
            <span className="num">
              {total === 0 ? 0 : page * PAGE + 1}–{Math.min(total, page * PAGE + PAGE)} de {total}
            </span>
          )}
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              disabled={live || page === 0}
              onClick={() => setPage(page - 1)}
            >
              Anterior
            </Button>
            <span className="num">{(live ? 0 : page) + 1}/{live ? 1 : pageCount}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              disabled={live || page >= pageCount - 1}
              onClick={() => setPage(page + 1)}
            >
              Siguiente
            </Button>
          </div>
        </div>
      </Panel>
    </div>
  )
}

/** Selector de una dimensión con toggle solo/excluir (mismo componente que llm-audit.tsx). */
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
            className={cn(
              "px-1.5 py-1.5",
              dim.mode === "include" ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:text-foreground",
            )}
            title="Ver solo este valor"
          >
            solo
          </button>
          <button
            onClick={() => onChange({ value: dim.value, mode: "exclude" })}
            className={cn(
              "px-1.5 py-1.5",
              dim.mode === "exclude" ? "bg-status-error/15 text-status-error" : "text-muted-foreground hover:text-foreground",
            )}
            title="Excluir este valor"
          >
            excluir
          </button>
        </div>
      )}
    </div>
  )
}
