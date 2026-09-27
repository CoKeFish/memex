import { useMemo, useState } from "react"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { fetchHackathones } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { Hackathon } from "@/types/domain"

const MODALITY_LABEL: Record<string, string> = {
  presencial: "Presencial",
  online: "Online",
  hibrido: "Híbrido",
  desconocido: "—",
}

/** Fecha del evento para ordenar/mostrar: usa starts_on, si no el deadline, si no created_at. */
function eventDate(h: Hackathon): string {
  return h.startsOn ?? h.registrationDeadline ?? h.createdAt.slice(0, 10)
}

export function HackathonesPage() {
  const { data, loading, error, reload } = useAsync<Hackathon[]>(() => fetchHackathones(), [])
  const all = data ?? []
  const [picked, setPicked] = useState<string>("todas")

  const modalities = useMemo(
    () => Array.from(new Set(all.map((h) => h.modality))).filter((m) => m !== "desconocido"),
    [all],
  )
  const rows = useMemo(() => {
    const filtered = picked === "todas" ? all : all.filter((h) => h.modality === picked)
    return [...filtered].sort((a, b) => eventDate(b).localeCompare(eventDate(a)))
  }, [all, picked])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · hackathones"
        title="Hackatones"
        description="Todo lo que extrajo el módulo hackathones desde tus correos, chats y redes: hackatones, datathons y retos de programación con fechas, modalidad, premios, tecnologías y requisitos. Cada fila enlaza a su evidencia y mensaje de origen."
        actions={
          modalities.length > 0 ? (
            <Select value={picked} onValueChange={setPicked}>
              <SelectTrigger className="h-8 w-auto min-w-[110px] text-xs" aria-label="Modalidad">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="todas" className="text-xs">Todas</SelectItem>
                {modalities.map((m) => (
                  <SelectItem key={m} value={m} className="text-xs">
                    {MODALITY_LABEL[m] ?? m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : undefined
        }
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando hackatones…
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Sin hackatones"
          hint="El módulo hackathones aún no extrajo hackatones. Verificá en Procesamiento que esté habilitado y que haya corrido sobre tus mensajes."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Hackatón</th>
                <th className="px-3 py-2 font-medium">Fechas</th>
                <th className="px-3 py-2 font-medium">Inscripción</th>
                <th className="px-3 py-2 font-medium">Modalidad</th>
                <th className="px-3 py-2 font-medium">Lugar</th>
                <th className="px-3 py-2 font-medium">Tecnologías</th>
                <th className="px-3 py-2 font-medium">Premios</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((h) => (
                <tr key={h.id} className="border-t align-top">
                  <td className="px-3 py-2">
                    <div className="font-medium">
                      {h.url ? (
                        <a
                          href={h.url}
                          target="_blank"
                          rel="noreferrer"
                          className="underline underline-offset-2 hover:text-primary"
                        >
                          {h.name}
                        </a>
                      ) : (
                        h.name
                      )}
                    </div>
                    {h.organizer && (
                      <div className="text-xs text-muted-foreground">{h.organizer}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 num whitespace-nowrap">
                    {h.startsOn ?? "—"}
                    {h.endsOn && h.endsOn !== h.startsOn ? ` → ${h.endsOn}` : ""}
                  </td>
                  <td className="px-3 py-2 num whitespace-nowrap">{h.registrationDeadline ?? "—"}</td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    {MODALITY_LABEL[h.modality] ?? h.modality}
                  </td>
                  <td className="px-3 py-2">{h.location || "—"}</td>
                  <td className="px-3 py-2">{h.technologies || "—"}</td>
                  <td className="px-3 py-2">{h.prizes || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
