import { Copy, FileText, Image as ImageIcon, ScanText, TriangleAlert } from "lucide-react"
import { StatusBadge } from "@/components/common/led"
import type { Tone } from "@/lib/status"
import type { MediaAsset, OcrStatus } from "@/types/domain"

const STATUS_TONE: Record<OcrStatus, Tone> = {
  ok: "ok",
  error: "error",
  skipped: "filtered",
  pending: "pending",
}
const STATUS_LABEL: Record<OcrStatus, string> = {
  ok: "OCR OK",
  error: "OCR error",
  skipped: "skipped",
  pending: "pendiente",
}

function bytes(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`
  if (n >= 1_000) return `${Math.round(n / 1_000)} KB`
  return `${n} B`
}

export function MediaOcr({ media }: { media: MediaAsset[] }) {
  return (
    <div className="mt-3 space-y-2.5">
      {media.map((m) => {
        const isPdf = m.contentType === "application/pdf"
        return (
          <div key={m.id} className="rounded-md border border-border bg-background/40">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
              {isPdf ? <FileText className="size-3.5 text-muted-foreground" /> : <ImageIcon className="size-3.5 text-muted-foreground" />}
              <span className="num text-xs font-medium">{m.filename ?? "(sin nombre)"}</span>
              <span className="num text-[11px] text-muted-foreground">
                {m.contentType} · {bytes(m.sizeBytes)}
              </span>
              <span className="ml-auto flex items-center gap-1.5">
                {m.dedupHit && (
                  <span className="inline-flex items-center gap-1 rounded border border-border bg-muted/40 px-1 py-0.5 text-[10px] text-muted-foreground" title="Misma imagen ya OCR-eada (dedup sha256) → 0 llamadas de visión">
                    <Copy className="size-3" /> dedup
                  </span>
                )}
                {m.truncated && (
                  <span className="inline-flex items-center gap-1 rounded border border-status-review/40 bg-status-review/10 px-1 py-0.5 text-[10px] font-medium text-status-review" title="finish_reason ≠ stop: transcripción cortada">
                    <TriangleAlert className="size-3" /> truncado
                  </span>
                )}
                <StatusBadge tone={STATUS_TONE[m.ocrStatus]} label={STATUS_LABEL[m.ocrStatus]} />
              </span>
            </div>
            <div className="grid gap-3 p-3 sm:grid-cols-[120px_1fr]">
              <div className="flex aspect-square items-center justify-center rounded border border-dashed border-border bg-muted/30 text-muted-foreground">
                <div className="text-center">
                  {isPdf ? <FileText className="mx-auto size-6" /> : <ImageIcon className="mx-auto size-6" />}
                  <div className="eyebrow mt-1">ref MinIO</div>
                </div>
              </div>
              <div className="min-w-0">
                <div className="eyebrow mb-1 flex items-center gap-1.5">
                  <ScanText className="size-3.5 text-brand" /> lo que vio el modelo multimodal
                </div>
                {m.ocrStatus === "ok" ? (
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded border border-border bg-muted/30 p-2 font-mono text-[11px] text-foreground">
                    {m.ocrText}
                  </pre>
                ) : m.ocrStatus === "skipped" ? (
                  <p className="text-xs text-muted-foreground">PDF — rasterizado a imagen pendiente; no pasó por el modelo de visión.</p>
                ) : m.ocrStatus === "error" ? (
                  <p className="font-mono text-xs text-status-error">{m.ocrError}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">pendiente de OCR</p>
                )}
                <div className="num mt-1.5 break-all text-[11px] text-muted-foreground">
                  sha256 {m.sha256}… · {m.bucket}/{m.objectKey} · {m.ocrModel ?? "—"} · {m.ocrAttempts} intento(s)
                  {m.dedupHit ? " · dedup (0 llamadas de visión)" : ""}
                </div>
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
