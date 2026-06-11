import { CalendarClock, Lock, MapPin, Repeat } from "lucide-react"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { StatusBadge } from "@/components/common/led"
import { formatDateOnly } from "@/lib/format"
import type { Tone } from "@/lib/status"
import type { CalendarConflict, ConsolidatedEventLite } from "@/types/domain"

const STATUS: Record<CalendarConflict["status"], { label: string; tone: Tone }> = {
  pending: { label: "pendiente", tone: "review" },
  resolved: { label: "resuelto", tone: "ok" },
  dismissed: { label: "descartado", tone: "neutral" },
}

function Side({ e }: { e: ConsolidatedEventLite }) {
  return (
    <div className="rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center gap-2">
        {e.protected && <Lock className="size-3.5 shrink-0 text-brand" />}
        <span className="text-sm font-medium">{e.title}</span>
      </div>
      <div className="num mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
        <span>{formatDateOnly(e.startsOn)}</span>
        <span>{e.startTime ? `${e.startTime}${e.endTime ? `–${e.endTime}` : ""}` : "todo el día"}</span>
        {e.location && (
          <span className="inline-flex items-center gap-0.5">
            <MapPin className="size-3" /> {e.location}
          </span>
        )}
        <span>
          rank {e.priorityRank}
          {e.protected ? " · protegido" : ""}
        </span>
      </div>
    </div>
  )
}

export function ConflictInspector({
  conflict,
  onClose,
}: {
  conflict: CalendarConflict | null
  onClose: () => void
}) {
  return (
    <Sheet
      open={!!conflict}
      onOpenChange={(o) => {
        if (!o) onClose()
      }}
    >
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto p-0 sm:max-w-md">
        {conflict && (
          <>
            <SheetHeader className="space-y-1 border-b border-border p-4">
              <div className="eyebrow">inspección · conflicto de horario</div>
              <SheetTitle className="flex items-center gap-2 text-left text-base">
                <CalendarClock className="size-4 shrink-0 text-status-review" />
                Choque de horario
              </SheetTitle>
              <SheetDescription className="num text-left">
                {conflict.recurring
                  ? `serie recurrente · se repite ${conflict.instanceCount}×`
                  : "choque puntual"}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-3 p-4">
              <div className="flex items-center gap-2">
                <StatusBadge tone={STATUS[conflict.status].tone} label={STATUS[conflict.status].label} />
                {conflict.recurring && (
                  <span className="inline-flex items-center gap-1 rounded border border-origin-provider/40 bg-origin-provider/10 px-1.5 py-0.5 text-[10px] font-medium text-origin-provider">
                    <Repeat className="size-3" /> ×{conflict.instanceCount}
                  </span>
                )}
              </div>
              <Side e={conflict.a} />
              <div className="text-center text-[11px] text-muted-foreground">se solapan en el tiempo</div>
              <Side e={conflict.b} />
              {conflict.recurring && (
                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  Se repite {conflict.instanceCount} veces entre {formatDateOnly(conflict.firstOn)} y{" "}
                  {formatDateOnly(conflict.lastOn)} (dos series recurrentes que coinciden). Arriba se muestra
                  la ocurrencia más próxima.
                </p>
              )}
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                Dos eventos distintos de alta importancia que ocupan el mismo horario. El calendario
                nunca los fusiona ni descarta uno — queda pendiente de revisión para que vos decidas.
              </p>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
