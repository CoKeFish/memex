import { useState } from "react"
import { ChevronLeft, ChevronRight, Lock } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { monthLongLabel } from "@/lib/format"
import { getCalendarEvents, NOW } from "@/data"
import type { ConsolidatedEvent } from "@/types/domain"

const WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

function eventColor(e: ConsolidatedEvent): string {
  if (e.protected) return "var(--brand)"
  if (e.origins.includes("provider")) return "var(--origin-provider)"
  if (e.origins.includes("module")) return "var(--origin-module)"
  return "var(--origin-inbox)"
}

function dateKey(dt: Date): string {
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`
}

export function MonthGrid({ onSelect }: { onSelect: (e: ConsolidatedEvent) => void }) {
  const events = getCalendarEvents()
  const [cursor, setCursor] = useState(() => new Date(NOW.getFullYear(), NOW.getMonth(), 1))
  const year = cursor.getFullYear()
  const month = cursor.getMonth()
  const first = new Date(year, month, 1)
  const startOffset = (first.getDay() + 6) % 7 // semana inicia lunes
  const gridStart = new Date(year, month, 1 - startOffset)
  const cells = Array.from({ length: 42 }, (_, i) => {
    const dt = new Date(gridStart)
    dt.setDate(gridStart.getDate() + i)
    return dt
  })

  const byDay = new Map<string, ConsolidatedEvent[]>()
  for (const e of events) {
    const arr = byDay.get(e.startsOn) ?? []
    arr.push(e)
    byDay.set(e.startsOn, arr)
  }
  const todayKey = dateKey(NOW)

  return (
    <Panel>
      <PanelHeader
        eyebrow="calendario · mes"
        title={<span className="capitalize">{monthLongLabel(cursor)}</span>}
        right={
          <div className="flex items-center gap-1">
            <Button variant="outline" size="icon" className="size-7" onClick={() => setCursor(new Date(year, month - 1, 1))} aria-label="Mes anterior">
              <ChevronLeft className="size-4" />
            </Button>
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => setCursor(new Date(NOW.getFullYear(), NOW.getMonth(), 1))}>
              Hoy
            </Button>
            <Button variant="outline" size="icon" className="size-7" onClick={() => setCursor(new Date(year, month + 1, 1))} aria-label="Mes siguiente">
              <ChevronRight className="size-4" />
            </Button>
          </div>
        }
      />
      <PanelBody className="p-2">
        <div className="grid grid-cols-7 gap-1">
          {WEEKDAYS.map((w) => (
            <div key={w} className="eyebrow px-2 py-1 text-center">{w}</div>
          ))}
          {cells.map((dt, i) => {
            const key = dateKey(dt)
            const inMonth = dt.getMonth() === month
            const evs = byDay.get(key) ?? []
            return (
              <div
                key={i}
                className={cn(
                  "min-h-[4.75rem] rounded-md border border-border/60 p-1",
                  !inMonth && "opacity-40",
                  key === todayKey && "ring-1 ring-brand/60",
                )}
              >
                <div className={cn("num mb-0.5 px-1 text-[11px]", key === todayKey ? "font-bold text-brand" : "text-muted-foreground")}>
                  {dt.getDate()}
                </div>
                <div className="space-y-0.5">
                  {evs.slice(0, 3).map((e) => (
                    <button
                      type="button"
                      key={e.id}
                      onClick={() => onSelect(e)}
                      className="flex w-full items-center gap-1 truncate rounded px-1 py-0.5 text-left text-[10px] transition hover:brightness-125"
                      style={{ background: `color-mix(in oklch, ${eventColor(e)} 16%, transparent)`, color: eventColor(e) }}
                      title={`${e.startTime ? e.startTime + " " : ""}${e.title}`}
                    >
                      {e.protected && <Lock className="size-2.5 shrink-0" />}
                      <span className="truncate">
                        {e.startTime ? `${e.startTime} ` : ""}
                        {e.title}
                      </span>
                    </button>
                  ))}
                  {evs.length > 3 && <div className="px-1 text-[10px] text-muted-foreground">+{evs.length - 3}</div>}
                </div>
              </div>
            )
          })}
        </div>
      </PanelBody>
    </Panel>
  )
}
