import { useState } from "react"
import { Panel } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchLogEvents, MODULES } from "@/data"
import { useAsync } from "@/lib/use-async"
import { LogRow } from "./log-row"
import type { LogLevel } from "@/types/domain"

const LEVELS: (LogLevel | "all")[] = ["all", "info", "warning", "error"]
const LABEL: Record<LogLevel | "all", string> = {
  all: "Todos",
  info: "Info",
  warning: "Warning",
  error: "Error",
}

/** Stream de eventos reconstruido de `llm_calls` (structlog no se persiste). Filtrable por nivel y
 * por módulo; el auto-refresco (topbar) re-dispara la carga vía `useAsync`. */
export function EventStream() {
  const [level, setLevel] = useState<LogLevel | "all">("all")
  const [moduleF, setModuleF] = useState<string>("all")
  const { data, loading, error, reload } = useAsync(
    () => fetchLogEvents({ module: moduleF === "all" ? undefined : moduleF, limit: 200 }),
    [moduleF],
  )
  const events = (data ?? []).filter((e) => level === "all" || e.level === level)

  return (
    <Panel className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border p-2">
        {LEVELS.map((l) => (
          <Button key={l} size="sm" variant={level === l ? "default" : "outline"} onClick={() => setLevel(l)}>
            {LABEL[l]}
          </Button>
        ))}
        <Select value={moduleF} onValueChange={setModuleF}>
          <SelectTrigger className="ml-1 h-8 w-auto min-w-[140px] text-xs" aria-label="Módulo">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all" className="text-xs">Todo módulo</SelectItem>
            {MODULES.map((m) => (
              <SelectItem key={m.key} value={m.key} className="text-xs">{m.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="num ml-auto self-center pr-1 text-xs text-muted-foreground">{events.length} eventos</span>
      </div>
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="space-y-2 p-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      ) : events.length === 0 ? (
        <EmptyState title="Sin eventos" hint="No hay llamadas registradas para este filtro." />
      ) : (
        <div className="max-h-[600px] overflow-y-auto">
          {events.map((e) => (
            <LogRow key={e.id} event={e} />
          ))}
        </div>
      )}
    </Panel>
  )
}
