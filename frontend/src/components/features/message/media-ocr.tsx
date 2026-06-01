// Panel de adjuntos del mensaje: por cada media_asset muestra tipo+ícono, tamaño, estado de OCR,
// la transcripción (lo que vio el modelo multimodal), y permite PREVISUALIZAR (imagen/PDF) y
// DESCARGAR el original (servido por /media/{id}). Para ZIPs lista las entradas internas y sus
// extensiones (del evento `zip-manifest` de la traza). Las marcas "truncado"/"omitido por límite"/
// "sin visión" se derivan de las llamadas OCR de la traza (purpose='ocr') del mismo sha256.

import { useEffect, useState } from "react"
import {
  Copy,
  Download,
  Eye,
  EyeOff,
  Loader2,
  ScanText,
  TriangleAlert,
} from "lucide-react"
import { StatusBadge } from "@/components/common/led"
import { fetchMediaBlobUrl } from "@/data"
import { ATTACHMENT_ICON, ATTACHMENT_LABEL, attachmentKind, type AttachmentKind } from "@/lib/attachment-kind"
import type { Tone } from "@/lib/status"
import type { InboxLlmCall, MediaAsset, OcrStatus } from "@/types/domain"

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

function rec(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {}
}
function str(v: unknown): string {
  return v == null ? "" : String(v)
}
function arr(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.map(rec) : []
}

interface ZipEntry {
  name: string
  ext: string
  kind: string
}

/** Datos de OCR derivados de la traza para UN asset (cruzando por metadata.sha256). */
interface AssetOcr {
  /** Llamadas de visión reales (con costo) sobre este asset. */
  visionCalls: number
  truncated: boolean
  /** OK pero sin ninguna llamada de visión → dedup o solo capa de texto. */
  noVision: boolean
  /** Imágenes omitidas por el tope (PDF con más imágenes que max_images). */
  skipped: { reason: string; maxImages: number }[]
  /** Entradas internas de un ZIP (del evento zip-manifest). */
  zipEntries: ZipEntry[]
  zipSkipped: ZipEntry[]
  zipTruncated: boolean
}

function deriveOcr(asset: MediaAsset, calls: InboxLlmCall[]): AssetOcr {
  const mine = calls.filter((c) => c.purpose === "ocr" && str(rec(c.metadata).sha256) === asset.sha256)
  const vision = mine.filter((c) => c.status !== "filtered")
  const skipped = mine
    .filter((c) => {
      const k = str(rec(c.metadata).kind)
      return c.status === "filtered" && (k === "pdf-skipped" || k === "zip-pdf-skipped")
    })
    .map((c) => ({
      reason: str(rec(c.metadata).skipped_reason),
      maxImages: Number(rec(c.metadata).max_images) || 0,
    }))
  const manifest = mine.find((c) => str(rec(c.metadata).kind) === "zip-manifest")
  const toEntry = (e: Record<string, unknown>): ZipEntry => ({
    name: str(e.name),
    ext: str(e.ext),
    kind: str(e.kind),
  })
  return {
    visionCalls: vision.length,
    truncated: asset.truncated === true || vision.some((c) => rec(c.metadata).truncated === true),
    noVision: asset.ocrStatus === "ok" && vision.length === 0,
    skipped,
    zipEntries: manifest ? arr(rec(manifest.metadata).entries).map(toEntry) : [],
    zipSkipped: manifest ? arr(rec(manifest.metadata).skipped).map(toEntry) : [],
    zipTruncated: manifest ? rec(manifest.metadata).truncated === true : false,
  }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** Previsualización lazy: baja el blob (con auth) al montarse y lo muestra como imagen o PDF. */
function MediaPreview({ asset, kind }: { asset: MediaAsset; kind: AttachmentKind }) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    let made: string | null = null
    fetchMediaBlobUrl(asset.id)
      .then((u) => {
        if (active) {
          made = u
          setUrl(u)
        } else {
          URL.revokeObjectURL(u)
        }
      })
      .catch((e) => active && setError(errMsg(e)))
    return () => {
      active = false
      if (made) URL.revokeObjectURL(made)
    }
  }, [asset.id])

  if (error) {
    return <p className="font-mono text-[11px] text-status-error">no se pudo cargar: {error}</p>
  }
  if (!url) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" /> cargando adjunto…
      </div>
    )
  }
  if (kind === "image") {
    return (
      <img
        src={url}
        alt={asset.filename ?? "adjunto"}
        className="max-h-96 max-w-full rounded border border-border bg-muted/20"
      />
    )
  }
  if (kind === "pdf") {
    return <iframe src={url} title={asset.filename ?? "pdf"} className="h-[28rem] w-full rounded border border-border" />
  }
  return (
    <a href={url} target="_blank" rel="noreferrer" className="text-[11px] text-brand underline">
      abrir adjunto en una pestaña nueva
    </a>
  )
}

function MediaCard({ asset, calls }: { asset: MediaAsset; calls: InboxLlmCall[] }) {
  const [showPreview, setShowPreview] = useState(false)
  const [showOcr, setShowOcr] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const kind = attachmentKind(asset.contentType, asset.extension ?? asset.filename)
  const Icon = ATTACHMENT_ICON[kind]
  const ocr = deriveOcr(asset, calls)
  const previewable = kind === "image" || kind === "pdf"

  async function download() {
    setDownloading(true)
    try {
      const u = await fetchMediaBlobUrl(asset.id, { download: true })
      const a = document.createElement("a")
      a.href = u
      a.download = asset.filename ?? `media-${asset.id}`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(u), 10_000)
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="rounded-md border border-border bg-background/40">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <Icon className="size-3.5 text-muted-foreground" />
        <span className="num text-xs font-medium">{asset.filename ?? "(sin nombre)"}</span>
        <span className="num text-[11px] text-muted-foreground">
          {ATTACHMENT_LABEL[kind]} · {asset.contentType} · {bytes(asset.sizeBytes)}
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-1.5">
          {ocr.skipped.length > 0 && (
            <span
              className="inline-flex items-center gap-1 rounded border border-status-review/40 bg-status-review/10 px-1 py-0.5 text-[10px] font-medium text-status-review"
              title={`Imágenes omitidas (${ocr.skipped[0].reason}); tope ${ocr.skipped[0].maxImages} imágenes por PDF`}
            >
              <TriangleAlert className="size-3" /> imágenes omitidas
            </span>
          )}
          {asset.dedupHit && (
            <span
              className="inline-flex items-center gap-1 rounded border border-border bg-muted/40 px-1 py-0.5 text-[10px] text-muted-foreground"
              title="Misma imagen ya OCR-eada (dedup sha256) → 0 llamadas de visión"
            >
              <Copy className="size-3" /> dedup
            </span>
          )}
          {ocr.truncated && (
            <span
              className="inline-flex items-center gap-1 rounded border border-status-review/40 bg-status-review/10 px-1 py-0.5 text-[10px] font-medium text-status-review"
              title="finish_reason ≠ stop: transcripción cortada"
            >
              <TriangleAlert className="size-3" /> truncado
            </span>
          )}
          <StatusBadge tone={STATUS_TONE[asset.ocrStatus]} label={STATUS_LABEL[asset.ocrStatus]} />
        </span>
      </div>

      <div className="space-y-3 p-3">
        {/* Acciones: previsualizar / descargar */}
        <div className="flex flex-wrap items-center gap-2">
          {previewable && (
            <button
              type="button"
              onClick={() => setShowPreview((v) => !v)}
              className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {showPreview ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
              {showPreview ? "ocultar" : kind === "pdf" ? "ver PDF" : "ver imagen"}
            </button>
          )}
          <button
            type="button"
            onClick={download}
            disabled={downloading}
            className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            {downloading ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
            descargar
          </button>
        </div>

        {showPreview && previewable && <MediaPreview asset={asset} kind={kind} />}

        {/* Texto transcrito por OCR — colapsado por defecto. */}
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => setShowOcr((v) => !v)}
            className="eyebrow flex items-center gap-1.5 hover:text-foreground"
          >
            <ScanText className="size-3.5 text-brand" /> lo que vio el modelo multimodal{" "}
            {showOcr ? "▾" : "▸"}
          </button>
          {showOcr && (
            <div className="mt-1">
              {asset.ocrStatus === "ok" ? (
                asset.ocrText.trim() ? (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded border border-border bg-muted/30 p-2 font-mono text-[11px] text-foreground">
                    {asset.ocrText}
                  </pre>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    OCR sin texto legible{ocr.noVision ? " (solo capa de texto / dedup, sin visión)" : ""}.
                  </p>
                )
              ) : asset.ocrStatus === "skipped" ? (
                <p className="text-xs text-muted-foreground">No pasó por OCR (tipo no soportado en este slice).</p>
              ) : asset.ocrStatus === "error" ? (
                <p className="font-mono text-xs text-status-error">{asset.ocrError ?? "error de OCR"}</p>
              ) : (
                <p className="text-xs text-muted-foreground">pendiente de OCR</p>
              )}
            </div>
          )}
        </div>

        {/* ZIP: entradas internas + extensiones */}
        {(ocr.zipEntries.length > 0 || ocr.zipSkipped.length > 0) && (
          <div className="min-w-0">
            <div className="eyebrow mb-1">contenido del ZIP{ocr.zipTruncated ? " (truncado por tope)" : ""}</div>
            <div className="flex flex-wrap gap-1.5">
              {ocr.zipEntries.map((e, i) => (
                <span
                  key={`e${i}`}
                  className="num inline-flex items-center gap-1 rounded border border-border bg-muted/30 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                  title={`${e.name} · ${e.kind}`}
                >
                  {e.name}
                  {e.ext && <span className="rounded bg-muted px-1 uppercase">{e.ext}</span>}
                </span>
              ))}
              {ocr.zipSkipped.map((e, i) => (
                <span
                  key={`s${i}`}
                  className="num inline-flex items-center gap-1 rounded border border-dashed border-border px-1.5 py-0.5 text-[10px] text-muted-foreground/70 line-through"
                  title="salteado (tipo no soportado / over-cap / zip anidado)"
                >
                  {e.name}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Footer técnico */}
        <div className="num break-all text-[11px] text-muted-foreground">
          sha256 {asset.sha256.slice(0, 16)}… · modelo {asset.ocrModel ?? "—"} · {asset.ocrAttempts} intento(s)
          {ocr.visionCalls > 0 ? ` · ${ocr.visionCalls} llamada(s) de visión` : ""}
          {ocr.noVision ? " · sin visión" : ""}
        </div>
      </div>
    </div>
  )
}

export function MediaOcr({ media, calls = [] }: { media: MediaAsset[]; calls?: InboxLlmCall[] }) {
  return (
    <div className="space-y-2.5">
      {media.map((m) => (
        <MediaCard key={m.id} asset={m} calls={calls} />
      ))}
    </div>
  )
}

export interface DeclaredAttachment {
  filename: string | null
  contentType: string
  size: number
}

/**
 * Adjuntos DECLARADOS en el correo (payload.attachments) que NO tienen media_asset: el correo los
 * trae pero no se almacenaron/OCR-earon (la fuente tiene `extract_media` apagado, o se ingirió
 * antes de habilitarlo). Se muestran igual para que el adjunto nunca quede invisible.
 */
export function UnstoredAttachments({ items }: { items: DeclaredAttachment[] }) {
  return (
    <div className="space-y-1.5">
      {items.map((a, i) => {
        const kind = attachmentKind(a.contentType, a.filename)
        const Icon = ATTACHMENT_ICON[kind]
        return (
          <div
            key={i}
            className="flex flex-wrap items-center gap-2 rounded-md border border-dashed border-border bg-muted/10 px-3 py-2"
          >
            <Icon className="size-3.5 text-muted-foreground" />
            <span className="num text-xs font-medium">{a.filename ?? "(sin nombre)"}</span>
            <span className="num text-[11px] text-muted-foreground">
              {ATTACHMENT_LABEL[kind]} · {a.contentType} · {bytes(a.size)}
            </span>
            <span
              className="ml-auto inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground"
              title="El correo declara este adjunto, pero no se almacenó en MinIO ni pasó por OCR (la fuente tiene extract_media apagado, o se ingirió antes de habilitarlo). Re-ingerí el correo con extract_media activo para procesarlo."
            >
              declarado · sin almacenar
            </span>
          </div>
        )
      })}
    </div>
  )
}
