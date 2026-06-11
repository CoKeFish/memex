import { useState } from "react"
import { PageHeader } from "@/components/common/page-header"
import { MonthGrid } from "@/components/features/calendar/month-grid"
import { Agenda, ConflictsList, DedupDecisions, SyncPanel } from "@/components/features/calendar/calendar-panels"
import { EventInspector } from "@/components/features/calendar/event-inspector"
import { ConflictInspector } from "@/components/features/calendar/conflict-inspector"
import { fetchCalendarEvents } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { CalendarConflict, ConsolidatedEvent } from "@/types/domain"

export function CalendarPage() {
  const [selected, setSelected] = useState<ConsolidatedEvent | null>(null)
  const [conflict, setConflict] = useState<CalendarConflict | null>(null)
  // El calendario mensual y la agenda comparten la misma capa consolidada (GET /calendar/events).
  const { data, loading, error, reload } = useAsync<ConsolidatedEvent[]>(
    () => fetchCalendarEvents(),
    [],
  )
  const events = data ?? []
  const eventsLoading = loading && !data
  // Al enfocar un conflicto: saltar el calendario a la ocurrencia y resaltar sus dos eventos.
  const focusDate = conflict ? conflict.a.startsOn : null
  const highlightIds = conflict ? [conflict.a.id, conflict.b.id] : undefined
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · calendar"
        title="Calendario"
        description="Todo el dominio del módulo calendar: la capa consolidada en un calendario mensual (clic en un evento para inspeccionarlo), los próximos eventos, los descartados/fusiones con su razón (automática del LLM o manual), los conflictos y el estado de sincronización con el proveedor."
      />
      <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
        <MonthGrid
          events={events}
          loading={eventsLoading}
          error={error}
          onSelect={setSelected}
          focusDate={focusDate}
          highlightIds={highlightIds}
        />
        <Agenda events={events} loading={eventsLoading} error={error} onSelect={setSelected} />
      </div>
      <div className="grid gap-5 lg:grid-cols-2 xl:grid-cols-3">
        <DedupDecisions />
        <ConflictsList onSelect={setConflict} />
        <SyncPanel onSynced={reload} />
      </div>
      <EventInspector event={selected} onClose={() => setSelected(null)} />
      <ConflictInspector conflict={conflict} onClose={() => setConflict(null)} />
    </div>
  )
}
