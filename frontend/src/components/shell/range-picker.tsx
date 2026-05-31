import { CalendarRange } from "lucide-react"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { RANGES, type RangeKey } from "@/lib/selectors"
import { useTimeRange } from "@/state/time-range"

export function RangePicker() {
  const { range, setRange } = useTimeRange()
  return (
    <Select value={range} onValueChange={(v) => setRange(v as RangeKey)}>
      <SelectTrigger className="h-8 gap-1.5 text-xs" aria-label="Rango temporal">
        <CalendarRange className="size-3.5 text-muted-foreground" />
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {RANGES.map((r) => (
          <SelectItem key={r.key} value={r.key} className="text-xs">
            {r.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
