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
  const winnerOrigin = event?.members.find((m) => m.isWinner)?.origin ?? event?.origins[0] ?? "extraction"
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
                {(event.placeName || event.location) && (
                  <Row
                    k="Lugar"
                    v={
                      event.placeName
                        ? [event.placeName, event.placeAddress].filter(Boolean).join(" — ")
                        : event.location
                    }
                  />
                )}
                {event.recurrenceHuman && (
                  <Row
                    k="Se repite"
                    v={event.recurrenceHuman + (event.occursOn ? ` · esta vez: ${formatDateOnly(event.occursOn)}` : "")}
                  />
                )}
                {event.originalStartOn && <Row k="Excepción de" v={formatDateOnly(event.originalStartOn)} />}
                <Row
                  k="Estado"
                  v={
                    event.commitment === "confirmado"
                      ? "confirmado: estás inscrito, pagaste o confirmaste"
                      : event.commitment === "provisional"
                        ? "provisional: te hablan como participante, sin confirmación"
                        : "anuncio: convocatoria o evento abierto; no es un compromiso tuyo"
                  }
                />
                {event.commitmentEvidence && <Row k="Por qué" v={`«${event.commitmentEvidence}»`} />}
                <Row k="Fijado" v={event.protected ? "sí: nada lo sobrescribe" : "no"} />
                <Row
                  k="Conflictos"
                  v={
                    event.protected || event.commitment === "confirmado" || event.commitment === "provisional"
                      ? "cuenta: si se solapa con otro evento tuyo, aparece como conflicto"
                      : "no cuenta (es un anuncio)"
                  }
                />
                <Row k="Origen" v={originLabel[winnerOrigin] ?? winnerOrigin} />
              </dl>
              <div className="num text-[11px] text-muted-foreground">
                técnico: rank {event.priorityRank}
                {event.seriesId ? ` · serie ${event.seriesId}` : ""}
              </div>

              {event.description && (
                <div>
                  <div className="eyebrow mb-1">resumen</div>
                  <p className="text-sm leading-relaxed text-foreground/90">{event.description}</p>
                </div>
              )}

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
                          <StatusBadge tone="ok" label="principal" />
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
                El evento toma fecha/hora de la fuente principal (determinista); el merge LLM combina título/lugar/descripción de las fuentes. Clic en un{" "}
                <span className="text-origin-inbox">inbox #</span> abre el camino de decisión de ese mensaje.
              </p>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
