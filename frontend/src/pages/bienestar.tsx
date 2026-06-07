import { useMemo, useState } from "react"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { KpiCard } from "@/components/common/kpi-card"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { RelativeTime } from "@/components/common/time"
import { HabitsPanel } from "@/components/features/bienestar/habits-panel"
import { fetchBienestarHabits, fetchBienestarRegistros, fetchBienestarSummary } from "@/data"
import type { BienestarHabit, BienestarRegistro, BienestarSummary } from "@/data/bienestar"
import { useAsync } from "@/lib/use-async"
import { formatInt } from "@/lib/format"

interface BienestarData {
  registros: BienestarRegistro[]
  summary: BienestarSummary
  habits: BienestarHabit[]
}

export function BienestarPage() {
  const { data, loading, error, reload } = useAsync<BienestarData>(async () => {
    const [registros, summary, habits] = await Promise.all([
      fetchBienestarRegistros(),
      fetchBienestarSummary(),
      fetchBienestarHabits(),
    ])
    return { registros, summary, habits }
  }, [])

  const [cat, setCat] = useState<string>("todas")

  const registros = data?.registros ?? []
  const habits = data?.habits ?? []
  const summary = data?.summary

  const categories = useMemo(
    () => Array.from(new Set(registros.map((r) => r.category))).sort(),
    [registros],
  )
  const filtered = cat === "todas" ? registros : registros.filter((r) => r.category === cat)

  const bestStreak = habits.reduce((m, h) => Math.max(m, h.streak), 0)
  const metToday = habits.filter((h) => h.metCurrent).length
  const maxCat = summary ? Math.max(1, ...Object.values(summary.byCategory)) : 1

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · bienestar"
        title="Salud y bienestar"
        description="Lo que registrás de tu día — comida, higiene, ejercicio, grooming y salud — más tus hábitos y su adherencia. Los registros los crea el agente; acá se ven."
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando bienestar…
        </div>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiCard eyebrow="registros" value={formatInt(summary?.total ?? 0)} />
            <KpiCard eyebrow="hábitos activos" value={formatInt(habits.length)} />
            <KpiCard eyebrow="mejor racha" value={formatInt(bestStreak)} accent />
            <KpiCard
              eyebrow="adherencia hoy"
              value={habits.length ? `${metToday}/${habits.length}` : "—"}
            />
          </div>

          <div className="grid gap-5 xl:grid-cols-2">
            <HabitsPanel habits={habits} onChanged={reload} />
            <Panel>
              <PanelHeader eyebrow="resumen" title="Por categoría" />
              <PanelBody className="space-y-2">
                {summary && Object.keys(summary.byCategory).length > 0 ? (
                  Object.entries(summary.byCategory)
                    .sort((a, b) => b[1] - a[1])
                    .map(([c, n]) => (
                      <div key={c} className="flex items-center gap-2">
                        <span className="w-24 shrink-0 truncate text-xs text-muted-foreground">
                          {c}
                        </span>
                        <div className="h-2 flex-1 rounded bg-muted/40">
                          <div
                            className="h-2 rounded bg-brand/70"
                            style={{ width: `${Math.round((n / maxCat) * 100)}%` }}
                          />
                        </div>
                        <span className="num w-8 shrink-0 text-right text-xs">{n}</span>
                      </div>
                    ))
                ) : (
                  <p className="text-sm text-muted-foreground">Sin registros en el período.</p>
                )}
              </PanelBody>
            </Panel>
          </div>

          <Panel>
            <PanelHeader
              eyebrow="registros"
              title="Actividad reciente"
              right={
                categories.length > 0 ? (
                  <Select value={cat} onValueChange={setCat}>
                    <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="todas" className="text-xs">
                        todas
                      </SelectItem>
                      {categories.map((c) => (
                        <SelectItem key={c} value={c} className="text-xs">
                          {c}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : undefined
              }
            />
            <PanelBody className="p-0">
              {filtered.length === 0 ? (
                <EmptyState title="Sin registros" hint="No hay registros para este filtro." />
              ) : (
                <div className="divide-y divide-border">
                  {filtered.map((r) => (
                    <div key={r.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
                      <div className="flex min-w-0 items-center gap-2">
                        <Badge variant="outline" className="shrink-0">
                          {r.category}
                        </Badge>
                        <span className="truncate text-sm text-foreground">
                          {r.activity || r.description || "—"}
                        </span>
                        {r.activity && r.description && (
                          <span className="hidden truncate text-xs text-muted-foreground sm:inline">
                            {r.description}
                          </span>
                        )}
                      </div>
                      <RelativeTime
                        date={r.occurredAt}
                        className="num shrink-0 text-xs text-muted-foreground"
                      />
                    </div>
                  ))}
                </div>
              )}
            </PanelBody>
          </Panel>
        </>
      )}
    </div>
  )
}
