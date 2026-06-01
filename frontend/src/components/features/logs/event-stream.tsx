import { useState } from "react"
import { Panel } from "@/components/common/panel"
import { EmptyState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { getLogEvents } from "@/data"
import { useAutoRefresh } from "@/state/auto-refresh"
import { LogRow } from "./log-row"
import type { LogLevel } from "@/types/domain"

const LEVELS: (LogLevel | "all")[] = ["all", "info", "warning", "error"]
const LABEL: Record<LogLevel | "all", string> = {
  all: "Todos",
  info: "Info",
  warning: "Warning",
  error: "Error",
}

/** Stream de eventos structlog (mock). En real sería un tail por API/CLI; el auto-refresco
 * (arriba en la topbar) re-evalúa la lista en cada tick. */
export function EventStream() {
  useAutoRefresh() // suscribe el re-render al tick de refresco
  const [level, setLevel] = useState<LogLevel | "all">("all")
  const events = getLogEvents().filter((e) => level === "all" || e.level === level)

  return (
    <Panel className="overflow-hidden">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border p-2">
        {LEVELS.map((l) => (
          <Button
            key={l}
            size="sm"
            variant={level === l ? "default" : "outline"}
            onClick={() => setLevel(l)}
          >
            {LABEL[l]}
          </Button>
        ))}
        <span className="num ml-auto self-center pr-1 text-xs text-muted-foreground">
          {events.length} eventos
        </span>
      </div>
      {events.length === 0 ? (
        <EmptyState title="Sin eventos" hint="No hay logs para este nivel." />
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
