import { useState } from "react"
import { Loader2 } from "lucide-react"
import { Link } from "react-router-dom"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { fetchFeedback, setFeedbackStatus } from "@/data"
import type { FeedbackEntry, FeedbackStatus, FeedbackStatusFilter } from "@/data"
import { useAsync } from "@/lib/use-async"
import { FEEDBACK_KIND_LABEL, FEEDBACK_STATUS_LABEL } from "@/lib/feedback"

const STATUS_FILTERS: { value: FeedbackStatusFilter; label: string }[] = [
  { value: "open", label: "Abierto" },
  { value: "reviewed", label: "Revisado" },
  { value: "dismissed", label: "Descartado" },
  { value: "all", label: "Todos" },
]

const STATUS_BADGE: Record<FeedbackStatus, "default" | "secondary" | "outline"> = {
  open: "default",
  reviewed: "secondary",
  dismissed: "outline",
}

/** Fecha+hora legible (corta el ISO a `YYYY-MM-DD HH:MM`). */
function whenDate(iso: string | null): string {
  return iso ? iso.slice(0, 16).replace("T", " ") : "—"
}

export function QualityPage() {
  const [status, setStatus] = useState<FeedbackStatusFilter>("open")
  const { data, loading, error, reload } = useAsync<FeedbackEntry[]>(
    () => fetchFeedback(status),
    [status],
  )
  const rows = data ?? []
  const [busyId, setBusyId] = useState<number | null>(null)

  async function move(inboxId: number, to: FeedbackStatus) {
    setBusyId(inboxId)
    try {
      await setFeedbackStatus(inboxId, to)
      reload()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="categoría · calidad"
        title="Calidad y precisión"
        description="Feedback manual que reportaste sobre el procesamiento de tus mensajes (resúmenes, extracción, OCR). Revisá cada caso, abrí su mensaje de origen y marcalo como revisado o descartado a medida que lo atendés. Es el insumo para calibrar filtros y parámetros."
        actions={
          <Select value={status} onValueChange={(v) => setStatus(v as FeedbackStatusFilter)}>
            <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs" aria-label="Estado">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_FILTERS.map((s) => (
                <SelectItem key={s.value} value={s.value} className="text-xs">
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando feedback…
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Sin feedback"
          hint="No hay feedback en este estado. Reportá un caso desde el detalle de un mensaje (botón “Reportar”) y aparecerá acá para calibrar."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Estado</th>
                <th className="px-3 py-2 font-medium">Mensaje</th>
                <th className="px-3 py-2 font-medium">Categorías</th>
                <th className="px-3 py-2 font-medium">Nota</th>
                <th className="px-3 py-2 font-medium">Actualizado</th>
                <th className="px-3 py-2 font-medium">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.inboxId} className="border-t align-top">
                  <td className="px-3 py-2">
                    <Badge variant={STATUS_BADGE[f.status]}>
                      {FEEDBACK_STATUS_LABEL[f.status]}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      to={`/datos/${f.inboxId}`}
                      className="font-medium underline underline-offset-2 hover:text-primary"
                    >
                      {f.subject || "(sin asunto)"}
                    </Link>
                    <div className="text-xs text-muted-foreground">
                      {f.fromEmail || "—"}
                      {f.tier ? ` · ${f.tier}` : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {f.kinds.map((k) => (
                        <span key={k} className="rounded bg-muted px-2 py-0.5 text-xs">
                          {FEEDBACK_KIND_LABEL[k]}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="max-w-xs px-3 py-2 whitespace-pre-wrap text-muted-foreground">
                    {f.note || "—"}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                    {whenDate(f.updatedAt)}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex gap-1 whitespace-nowrap">
                      {f.status !== "reviewed" && (
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={busyId === f.inboxId}
                          onClick={() => void move(f.inboxId, "reviewed")}
                        >
                          Revisado
                        </Button>
                      )}
                      {f.status !== "dismissed" && (
                        <Button
                          size="xs"
                          variant="ghost"
                          disabled={busyId === f.inboxId}
                          onClick={() => void move(f.inboxId, "dismissed")}
                        >
                          Descartar
                        </Button>
                      )}
                      {f.status !== "open" && (
                        <Button
                          size="xs"
                          variant="ghost"
                          disabled={busyId === f.inboxId}
                          onClick={() => void move(f.inboxId, "open")}
                        >
                          Reabrir
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
