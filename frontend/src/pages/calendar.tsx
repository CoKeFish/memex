import { useState } from "react"
import { PageHeader } from "@/components/common/page-header"
import { MonthGrid } from "@/components/features/calendar/month-grid"
import { Agenda, ConflictsList, DedupDecisions, SyncPanel } from "@/components/features/calendar/calendar-panels"
import { EventInspector } from "@/components/features/calendar/event-inspector"
import type { ConsolidatedEvent } from "@/types/domain"

export function CalendarPage() {
  const [selected, setSelected] = useState<ConsolidatedEvent | null>(null)
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · calendar"
        title="Calendario"
        description="Todo el dominio del módulo calendar: la capa consolidada en un calendario mensual (clic en un evento para inspeccionarlo), los próximos eventos, los descartados/fusiones con su razón (automática del LLM o manual), los conflictos y el estado de sincronización con el proveedor."
      />
      <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
        <MonthGrid onSelect={setSelected} />
        <Agenda onSelect={setSelected} />
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
