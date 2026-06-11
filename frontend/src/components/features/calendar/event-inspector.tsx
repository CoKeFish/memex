import { ArrowUpRight, Lock } from "lucide-react"
import { Link } from "react-router-dom"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { StatusBadge } from "@/components/common/led"
import { formatDateOnly } from "@/lib/format"
import { originChart, originLabel } from "@/lib/status"
import type { CalendarOutcome, ConsolidatedEvent } from "@/types/domain"

const OUTCOME_LABEL: Record<CalendarOutcome, string> = {
  unique: "único",
  duplicate: "duplicado",
  shadowed: "opacado",
  conflict: "conflicto",
  echo: "echo",
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right text-foreground">{v}</dd>
    </div>
  )
}

export function EventInspector({ event, onClose }: { event: ConsolidatedEvent | null; onClose: () => void }) {
  return (
    <Sheet open={!!event} onOpenChange={(o) => { if (!o) onClose() }}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto p-0 sm:max-w-md">
        {event && (
          <>
            <SheetHeader className="space-y-1 border-b border-border p-4">
              <div className="eyebrow">inspección · evento consolidado</div>
              <SheetTitle className="flex items-center gap-2 text-left text-base">
                {event.protected && <Lock className="size-4 shrink-0 text-brand" />}
                {event.title}
              </SheetTitle>
              <SheetDescription className="num text-left">mod_calendar_consolidated #{event.id}</SheetDescription>
            </SheetHeader>
            <div className="space-y-4 p-4">
              <dl className="num space-y-1.5 text-sm">
                <Row k="Fecha" v={formatDateOnly(event.startsOn) + (event.endsOn ? ` – ${formatDateOnly(event.endsOn)}` : "")} />
                <Row k="Horario" v={event.startTime ? `${event.startTime}${event.endTime ? `–${event.endTime}` : ""}` : "todo el día"} />
                {event.location && <Row k="Lugar" v={event.location} />}
                <Row k="Prioridad" v={`rank ${event.priorityRank}${event.protected ? " · protegido" : ""}`} />
              </dl>

              <div>
                <div className="eyebrow mb-2">compuesto por {event.members.length} crudo(s) · event_links</div>
                <ul className="space-y-2">
                  {event.members.map((m) => (
                    <li key={m.id} className="rounded-md border border-border bg-background/40 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-2 text-sm">
                          <span className="size-2 rounded-full" style={{ background: originChart[m.origin] }} />
                          <span className="font-medium">{originLabel[m.origin]}</span>
                          {m.provider && <span className="num text-[11px] text-muted-foreground">{m.provider}</span>}
                        </span>
                        {m.isWinner ? (
                          <StatusBadge tone="ok" label="ganador" />
                        ) : (
                          <span className="num text-[11px] text-muted-foreground">{OUTCOME_LABEL[m.processingOutcome]}</span>
                        )}
                      </div>
                      {m.evidence && <p className="mt-1.5 text-xs text-muted-foreground">{m.evidence}</p>}
                      {m.sourceInboxIds.length > 0 && (
                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                          {m.sourceInboxIds.map((id) => (
                            <Link
                              key={id}
                              to={`/datos/${id}`}
                              className="num inline-flex items-center gap-0.5 rounded bg-origin-inbox/10 px-1.5 py-0.5 text-[11px] text-origin-inbox hover:bg-origin-inbox/20"
                            >
                              inbox #{id} <ArrowUpRight className="size-3" />
                            </Link>
                          ))}
                        </div>
                      )}
                      {m.origin === "provider" && (
                        <p className="mt-1 text-[11px] text-muted-foreground">Evento estructurado del proveedor (no pasa por inbox/LLM).</p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>

              <p className="text-[11px] leading-relaxed text-muted-foreground">
                El consolidado toma fecha/hora del ganador (determinista); el merge LLM combina título/lugar/descripción de los miembros. Clic en un{" "}
                <span className="text-origin-inbox">inbox #</span> abre el camino de decisión de ese mensaje.
              </p>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
