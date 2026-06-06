// Lista de media (media_assets) contra la API real: GET /media. Para el monitor /ocr. El blob de
// cada adjunto se sigue sirviendo aparte por GET /media/{id} (ver fetchMediaBlobUrl en ./email).

import { apiGet } from "@/lib/api"
import type { MediaAsset, OcrStatus } from "@/types/domain"

export type OcrStatusFilter = OcrStatus | "all"

/** Un media_asset + contexto de su mensaje (asignable a MediaAsset para reusar <MediaOcr>). */
export interface MediaListEntry extends MediaAsset {
  inboxId: number
  subject: string | null
  occurredAt: string | null
}

interface MediaListItemApi {
  id: number
  sha256: string
  content_type: string
  filename: string | null
  extension: string | null
  size_bytes: number
  ocr_status: string
  ocr_model: string | null
  ocr_text: string | null
  ocr_error: string | null
  ocr_attempts: number
  ocr_done_at?: string | null
  inbox_id: number
  subject?: string | null
  occurred_at?: string | null
}

interface MediaListApi {
  items: MediaListItemApi[]
  next_cursor: number | null
}

function toEntry(m: MediaListItemApi): MediaListEntry {
  return {
    id: m.id,
    sha256: m.sha256,
    contentType: m.content_type,
    sizeBytes: m.size_bytes,
    filename: m.filename,
    extension: m.extension,
    ocrStatus: m.ocr_status as OcrStatus,
    ocrModel: m.ocr_model,
    ocrText: m.ocr_text ?? "",
    ocrError: m.ocr_error,
    ocrAttempts: m.ocr_attempts,
    inboxId: m.inbox_id,
    subject: m.subject ?? null,
    occurredAt: m.occurred_at ?? null,
  }
}

/** Lista los media_assets del usuario (más nuevos primero), opcionalmente por estado OCR. */
export async function fetchMediaList(opts?: {
  ocrStatus?: OcrStatusFilter
  max?: number
}): Promise<MediaListEntry[]> {
  const max = opts?.max ?? 500
  const pageSize = 100
  const out: MediaListEntry[] = []
  let cursor: number | null = null
  while (out.length < max) {
    const qs = new URLSearchParams()
    if (opts?.ocrStatus && opts.ocrStatus !== "all") qs.set("ocr_status", opts.ocrStatus)
    qs.set("limit", String(pageSize))
    if (cursor != null) qs.set("cursor", String(cursor))
    const page = await apiGet<MediaListApi>(`/media?${qs.toString()}`)
    out.push(...page.items.map(toEntry))
    if (page.next_cursor == null || page.items.length === 0) break
    cursor = page.next_cursor
  }
  return out
}
