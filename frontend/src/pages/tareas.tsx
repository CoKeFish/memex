// /tareas — todas las tareas del usuario, con filtro por estado y texto y dos formas de ordenarlas.
// El detalle por asunto vive en /asuntos/:id (panel «tareas»); acá se ve el conjunto.
//
// Agrupar (pedido del dueño, 2026-09-15): una tira de 26 filas no contesta «¿qué hago ahora?», y
// además repetía el nombre del asunto trece veces seguidas porque trece eran del mismo curso.
// Por CUÁNDO contesta esa pregunta; por ASUNTO sirve para sentarse a despachar un curso entero, y
// ahí el nombre va en el separador y no en cada fila.

import { useMemo, useState } from "react"
import { ListChecks } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { TareasList } from "@/components/features/tareas/tareas-list"
import { fetchTareas, type Tarea, type TareaStatus } from "@/data"
import { useAsync } from "@/lib/use-async"
import { TAREA_STATUSES, TAREA_STATUS_LABEL } from "@/lib/tareas"
import {
  GROUP_BY_LABEL,
  SORT_BY_LABEL,
  type TareasGroupBy,
  type TareasSortBy,
} from "@/lib/tareas-group"

type StatusFilter = TareaStatus | "all"

export function TareasPage() {
  const [status, setStatus] = useState<StatusFilter>("pendiente")
  const [q, setQ] = useState("")
  const [groupBy, setGroupBy] = useState<TareasGroupBy>("cuando")
  const [sortBy, setSortBy] = useState<TareasSortBy>("fecha")
  const { data, loading, error, reload } = useAsync<Tarea[]>(
    () => fetchTareas({ status }),
    [status],
  )
  const tareas = useMemo(() => {
    const all = data ?? []
    const needle = q.trim().toLowerCase()
    if (!needle) return all
    return all.filter(
      (t) =>
        t.title.toLowerCase().includes(needle) ||
        t.description.toLowerCase().includes(needle) ||
        (t.asuntoName ?? "").toLowerCase().includes(needle),
    )
  }, [data, q])
  const vencidas = tareas.filter((t) => t.status === "pendiente" && t.overdue).length

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Dominios"
        title="Tareas"
        description="Lo que hay que hacer, sacado de los mensajes (cursos, trámites, proyectos) o anotado a mano. Cada tarea cuelga de su asunto; las de curso «para la próxima clase» vencen solas cuando la clase pasa."
      />
      <Panel>
        <PanelHeader
          eyebrow="lista"
          title={
            loading && !data
              ? "Cargando…"
              : `${tareas.length} ${status === "all" ? "tareas" : TAREA_STATUS_LABEL[status].toLowerCase() + "s"}${vencidas ? ` · ${vencidas} con fecha vencida` : ""}`
          }
          right={
            <div className="flex flex-wrap items-center gap-2">
              <Select value={status} onValueChange={(v) => setStatus(v as StatusFilter)}>
                <SelectTrigger size="sm" className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TAREA_STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>
                      {TAREA_STATUS_LABEL[s]}s
                    </SelectItem>
                  ))}
                  <SelectItem value="all">Todas</SelectItem>
                </SelectContent>
              </Select>
              <Select value={groupBy} onValueChange={(v) => setGroupBy(v as TareasGroupBy)}>
                <SelectTrigger size="sm" className="w-36" aria-label="Agrupar">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(GROUP_BY_LABEL) as TareasGroupBy[]).map((g) => (
                    <SelectItem key={g} value={g}>
                      {GROUP_BY_LABEL[g]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={sortBy} onValueChange={(v) => setSortBy(v as TareasSortBy)}>
                <SelectTrigger size="sm" className="w-40" aria-label="Ordenar">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(SORT_BY_LABEL) as TareasSortBy[]).map((o) => (
                    <SelectItem key={o} value={o}>
                      {SORT_BY_LABEL[o]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="filtrar por texto o asunto"
                className="h-8 w-56 text-sm"
              />
            </div>
          }
        />
        <PanelBody>
          {error ? (
            <ErrorState detail={error} onRetry={reload} />
          ) : loading && !data ? (
            <Skeleton className="h-24 w-full" />
          ) : tareas.length === 0 ? (
            <EmptyState
              icon={<ListChecks className="size-5" />}
              title="Sin tareas en este filtro"
              hint="Las tareas nacen de los mensajes de cursos, trámites y proyectos, o con «memex tareas add»."
            />
          ) : (
            <TareasList
              tareas={tareas}
              onChanged={reload}
              groupBy={groupBy}
              sortBy={sortBy}
              /* Agrupado por asunto el nombre ya está en el separador: repetirlo en cada fila es
                 justo el ruido que se vino a quitar. */
              showAsunto={groupBy !== "asunto"}
            />
          )}
        </PanelBody>
      </Panel>
    </div>
  )
}
