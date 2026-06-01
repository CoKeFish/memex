import { cn } from "@/lib/utils"
import { RelativeTime } from "@/components/common/time"
import type { LogEvent, LogLevel } from "@/types/domain"

const LEVEL: Record<LogLevel, { cls: string; label: string }> = {
  info: { cls: "text-status-ok", label: "INFO" },
  warning: { cls: "text-status-review", label: "WARN" },
  error: { cls: "text-status-error", label: "ERROR" },
}

/** Una línea de log structlog: nivel + evento + módulo + correlación + fields. */
export function LogRow({ event }: { event: LogEvent }) {
  const lvl = LEVEL[event.level]
  return (
    <div className="border-b border-border px-4 py-2 font-mono text-[11px]">
      <div className="flex items-center gap-2">
        <span className={cn("font-semibold", lvl.cls)}>{lvl.label}</span>
        <span className="text-foreground">{event.event}</span>
        <span className="text-muted-foreground">· {event.module}</span>
        <span className="ml-auto shrink-0 text-muted-foreground">
          <RelativeTime date={event.ts} />
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-muted-foreground">
        {event.requestId && <span>req {event.requestId.slice(0, 8)}</span>}
        {event.runId && <span>run {event.runId}</span>}
        {event.sourceId != null && <span>source {event.sourceId}</span>}
        {event.inboxId != null && <span>inbox #{event.inboxId}</span>}
        {Object.entries(event.fields)
          .slice(0, 6)
          .map(([k, v]) => (
            <span key={k}>
              {k}=<span className="text-foreground">{String(v)}</span>
            </span>
          ))}
      </div>
    </div>
  )
}
