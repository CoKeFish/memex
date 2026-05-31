import { useState } from "react"
import { Bell, CheckCheck } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { useAlerts } from "@/state/alerts"
import type { Tone } from "@/lib/status"
import type { AlertSeverity } from "@/types/domain"

const sevTone: Record<AlertSeverity, Tone> = { critica: "error", alta: "review", info: "running" }

export function AlertBell() {
  const { alerts, unread, markRead, markAllRead } = useAlerts()
  const nav = useNavigate()
  const [open, setOpen] = useState(false)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" className="relative size-8" aria-label={`Alertas, ${unread} sin leer`}>
          <Bell className="size-4" />
          {unread > 0 && (
            <span className="num absolute -right-0.5 -top-0.5 flex min-w-4 items-center justify-center rounded-full bg-status-error px-1 text-[10px] font-semibold leading-4 text-white">
              {unread}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[360px] p-0">
        <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
          <span className="eyebrow">Alertas · {unread} sin leer</span>
          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={markAllRead}>
            <CheckCheck className="size-3.5" /> Marcar todas
          </Button>
        </div>
        <ScrollArea className="max-h-[340px]">
          <ul className="divide-y divide-border">
            {alerts.map((a) => (
              <li key={a.id}>
                <button
                  onClick={() => {
                    markRead(a.id)
                    setOpen(false)
                    nav(a.deepLink)
                  }}
                  className={cn(
                    "flex w-full gap-3 px-3 py-2.5 text-left hover:bg-accent/50",
                    !a.read && "bg-brand/5",
                  )}
                >
                  <Led tone={sevTone[a.severity]} className="mt-1.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className={cn("truncate text-sm", a.read ? "text-muted-foreground" : "font-medium text-foreground")}>
                        {a.title}
                      </span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">
                        <RelativeTime date={a.at} />
                      </span>
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{a.detail}</p>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </ScrollArea>
      </PopoverContent>
    </Popover>
  )
}
