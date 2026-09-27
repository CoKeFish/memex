// Documentos (router /documentos): subir un archivo (PDF, ZIP, imagen) y, opcionalmente, dejarlo
// como evidencia de un asunto (enriquecerlo con las diapositivas, el programa, un PDF).

import { apiUpload } from "@/lib/api"

export interface DocumentoUpload {
  inboxId: number | null
  filename: string
  duplicado: boolean
  asuntoId: number | null
  leido: boolean
  items: number
  costUsd: number
  error?: string
}

interface DocumentoUploadApi {
  inbox_id: number | null
  filename: string
  duplicado: boolean
  asunto_id?: number | null
  leido: boolean
  items: number
  cost_usd: number
  error?: string
}

export async function uploadDocumento(
  file: File,
  opts: { asuntoId?: number; nota?: string; procesar?: boolean } = {},
): Promise<DocumentoUpload> {
  const form = new FormData()
  form.append("archivo", file, file.name)
  form.append("nota", opts.nota ?? "")
  form.append("procesar", String(opts.procesar ?? true))
  if (opts.asuntoId != null) form.append("asunto_id", String(opts.asuntoId))
  const r = await apiUpload<DocumentoUploadApi>("/documentos", form)
  return {
    inboxId: r.inbox_id,
    filename: r.filename,
    duplicado: r.duplicado,
    asuntoId: r.asunto_id ?? null,
    leido: r.leido,
    items: r.items,
    costUsd: r.cost_usd,
    error: r.error,
  }
}
