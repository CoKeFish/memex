import { useState } from "react"
import { PageHeader } from "@/components/common/page-header"
import { MonthGrid } from "@/components/features/calendar/month-grid"
import { Agenda, ConflictsList, DedupDecisions, SyncPanel } from "@/components/features/calendar/calendar-panels"
import { EventInspector } from "@/components/features/calendar/event-inspector"
import { fetchCalendarEvents } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { ConsolidatedEvent } from "@/types/domain"

export function CalendarPage() {
  const [selected, setSelected] = useState<ConsolidatedEvent | null>(null)
  // El calendario mensual y la agenda comparten la misma capa consolidada (GET /calendar/events).
  const { data, loading, error } = useAsync<ConsolidatedEvent[]>(() => fetchCalendarEvents(), [])
  const events = data ?? []
  const eventsLoading = loading && !data
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · calendar"
        title="Calendario"
        description="Todo el dominio del módulo calendar: la capa consolidada en un calendario mensual (clic en un evento para inspeccionarlo), los próximos eventos, los descartados/fusiones con su razón (automática del LLM o manual), los conflictos y el estado de sincronización con el proveedor."
      />
      <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
        <MonthGrid events={events} loading={eventsLoading} error={error} onSelect={setSelected} />
        <Agenda events={events} loading={eventsLoading} error={error} onSelect={setSelected} />
      </div>
      <div className="grid gap-5 lg:grid-cols-2 xl:grid-cols-3">
        <DedupDecisions />
        <ConflictsList />
        <SyncPanel />
      </div>
      <EventInspector event={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
