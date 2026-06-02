import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { RelativeTime } from "@/components/common/time"
import type { LogEventRow, LogLevel } from "@/types/domain"

// Estilo por nivel (colores del sistema de status). `debug` apaga; `critical` resalta como error.
const LEVEL: Record<LogLevel, { cls: string; label: string }> = {
  debug: { cls: "text-muted-foreground", label: "DEBUG" },
  info: { cls: "text-status-ok", label: "INFO" },
  warning: { cls: "text-status-review", label: "WARN" },
  error: { cls: "text-status-error", label: "ERROR" },
  critical: { cls: "text-status-error", label: "CRIT" },
}

/** Una línea de `log_events`: nivel + evento + logger + ts relativo + chips de correlación REALES
 *  (request_id/run_id/source_id/inbox_id). El chip "req" salta a filtrar por ese request_id (la
 *  traza de un mensaje). Click en la fila expande un <pre> con los `fields` y la exception. */
export function LogRow({
  event,
  onFilterRequest,
}: {
  event: LogEventRow
  onFilterRequest?: (requestId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const lvl = LEVEL[event.level] ?? LEVEL.info
  const fieldKeys = Object.keys(event.fields)
  const hasDetail = fieldKeys.length > 0 || event.exception != null

  return (
    <div className="border-b border-border font-mono text-[11px]">
      <button
        type="button"
        onClick={() => hasDetail && setOpen((o) => !o)}
        className={cn(
          "flex w-full items-start gap-2 px-4 py-2 text-left",
          hasDetail ? "hover:bg-accent/30" : "cursor-default",
        )}
        aria-expanded={hasDetail ? open : undefined}
      >
        <ChevronRight
          className={cn(
            "mt-0.5 size-3 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
            !hasDetail && "opacity-0",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className={cn("shrink-0 font-semibold", lvl.cls)}>{lvl.label}</span>
            <span className="truncate text-foreground">{event.event}</span>
            {event.logger && <span className="shrink-0 text-muted-foreground">· {event.logger}</span>}
            <span className="ml-auto shrink-0 text-muted-foreground">
              <RelativeTime date={event.ts} />
            </span>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-muted-foreground">
            {event.requestId && (
              <span
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation()
                  onFilterRequest?.(event.requestId!)
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault()
                    e.stopPropagation()
                    onFilterRequest?.(event.requestId!)
                  }
                }}
                className="rounded text-brand hover:underline"
                title="Filtrar por este request_id (ver la traza del mensaje)"
              >
                req {event.requestId.slice(0, 8)}
              </span>
            )}
            {event.runId && <span>run {event.runId.slice(0, 8)}</span>}
            {event.sourceId != null && <span>source {event.sourceId}</span>}
            {event.inboxId != null && (
              <Link
                to={`/datos/${event.inboxId}`}
                onClick={(e) => e.stopPropagation()}
                className="text-origin-inbox hover:underline"
              >
                inbox #{event.inboxId}
              </Link>
            )}
            {fieldKeys.slice(0, 6).map((k) => (
              <span key={k}>
                {k}=<span className="text-foreground">{String(event.fields[k])}</span>
              </span>
            ))}
          </div>
        </div>
      </button>
      {open && hasDetail && (
        <div className="space-y-2 border-t border-border bg-muted/20 px-4 py-2.5 pl-9">
          {fieldKeys.length > 0 && (
            <pre className="overflow-x-auto whitespace-pre-wrap break-words text-[10.5px] leading-relaxed text-muted-foreground">
              {JSON.stringify(event.fields, null, 2)}
            </pre>
          )}
          {event.exception && (
            <pre className="overflow-x-auto whitespace-pre-wrap break-words text-[10.5px] leading-relaxed text-status-error">
              {event.exception}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
