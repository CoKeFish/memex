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
import { MediaOcr } from "@/components/features/message/media-ocr"
import { fetchMediaList, reprocessInboxItem } from "@/data"
import type { MediaListEntry, OcrStatusFilter } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { OcrStatus } from "@/types/domain"

const OCR_STATUS_LABEL: Record<OcrStatus, string> = {
  pending: "Pendiente",
  ok: "OK",
  error: "Error",
  skipped: "Omitido",
}

const OCR_STATUS_BADGE: Record<OcrStatus, "default" | "secondary" | "destructive" | "outline"> = {
  pending: "secondary",
  ok: "default",
  error: "destructive",
  skipped: "outline",
}

const FILTERS: { value: OcrStatusFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "pending", label: "Pendiente" },
  { value: "ok", label: "OK" },
  { value: "error", label: "Error" },
  { value: "skipped", label: "Omitido" },
]

export function OcrMediaPage() {
  const [ocrStatus, setOcrStatus] = useState<OcrStatusFilter>("all")
  const { data, loading, error, reload } = useAsync<MediaListEntry[]>(
    () => fetchMediaList({ ocrStatus }),
    [ocrStatus],
  )
  const rows = data ?? []
  const [busyId, setBusyId] = useState<number | null>(null)

  async function reocr(inboxId: number) {
    setBusyId(inboxId)
    try {
      await reprocessInboxItem(inboxId, ["ocr"], true)
      reload()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="categoría · multimedia"
        title="Multimedia / OCR"
        description="Adjuntos (imágenes y PDF) guardados en MinIO y su estado de OCR. Cada tarjeta muestra el texto que extrajo el modelo frente a la imagen original, con enlace a su mensaje. «Re-OCR» vuelve a transcribir los adjuntos de ese mensaje."
        actions={
          <Select value={ocrStatus} onValueChange={(v) => setOcrStatus(v as OcrStatusFilter)}>
            <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs" aria-label="Estado OCR">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FILTERS.map((f) => (
                <SelectItem key={f.value} value={f.value} className="text-xs">
                  {f.label}
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
          <Loader2 className="size-4 animate-spin" /> Cargando adjuntos…
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Sin adjuntos"
          hint="No hay media en este estado. El OCR corre sobre imágenes y PDF de tus mensajes; verificá en Procesamiento que el worker de OCR esté habilitado."
        />
      ) : (
        <div className="space-y-4">
          {rows.map((m) => (
            <div key={m.id} className="space-y-2 rounded-lg border p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <Link
                    to={`/datos/${m.inboxId}`}
                    className="font-medium underline underline-offset-2 hover:text-primary"
                  >
                    {m.subject || "(sin asunto)"}
                  </Link>
                  <div className="text-xs text-muted-foreground">
                    {m.filename || m.contentType}
                    {m.occurredAt ? ` · ${m.occurredAt.slice(0, 10)}` : ""}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge variant={OCR_STATUS_BADGE[m.ocrStatus]}>
                    {OCR_STATUS_LABEL[m.ocrStatus]}
                  </Badge>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={busyId === m.inboxId}
                    onClick={() => void reocr(m.inboxId)}
                  >
                    {busyId === m.inboxId ? "Re-OCR…" : "Re-OCR"}
                  </Button>
                </div>
              </div>
              <MediaOcr media={[m]} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
