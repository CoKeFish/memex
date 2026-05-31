import { RotateCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { LiveSince } from "@/components/common/time"
import { REFRESH_OPTIONS, useAutoRefresh, type IntervalSec } from "@/state/auto-refresh"

export function RefreshControl() {
  const { intervalSec, setIntervalSec, refreshNow } = useAutoRefresh()
  return (
    <div className="flex items-center gap-1.5">
      <span className="hidden lg:inline">
        <LiveSince />
      </span>
      <Button variant="ghost" size="icon" className="size-8" onClick={refreshNow} title="Refrescar ahora">
        <RotateCw className="size-4" />
      </Button>
      <Select value={String(intervalSec)} onValueChange={(v) => setIntervalSec(Number(v) as IntervalSec)}>
        <SelectTrigger className="h-8 w-[78px] text-xs" aria-label="Auto-refresco">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {REFRESH_OPTIONS.map((o) => (
            <SelectItem key={o.value} value={String(o.value)} className="text-xs">
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
