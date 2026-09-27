// Clasificación de adjuntos en un "tipo" con su ícono/etiqueta, compartida entre la vista datos
// (íconos por fila) y el detalle (panel de adjuntos). Deriva del content-type (preferido) y, como
// respaldo, de la extensión del nombre. Espeja a grandes rasgos los tipos que el OCR distingue.

import {
  Archive,
  File,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Music,
  Presentation,
  Video,
  type LucideIcon,
} from "lucide-react"

export type AttachmentKind =
  | "pdf"
  | "image"
  | "zip"
  | "doc"
  | "sheet"
  | "slides"
  | "audio"
  | "video"
  | "text"
  | "file"

export const ATTACHMENT_ICON: Record<AttachmentKind, LucideIcon> = {
  pdf: FileText,
  image: ImageIcon,
  zip: Archive,
  doc: FileText,
  sheet: FileSpreadsheet,
  slides: Presentation,
  audio: Music,
  video: Video,
  text: FileText,
  file: File,
}

export const ATTACHMENT_LABEL: Record<AttachmentKind, string> = {
  pdf: "PDF",
  image: "Imagen",
  zip: "ZIP",
  doc: "Documento",
  sheet: "Hoja de cálculo",
  slides: "Presentación",
  audio: "Audio",
  video: "Video",
  text: "Texto",
  file: "Archivo",
}

const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "heic", "svg"])
const ARCHIVE_EXT = new Set(["zip", "rar", "7z", "tar", "gz", "tgz"])
const AUDIO_EXT = new Set(["mp3", "wav", "ogg", "m4a", "flac", "aac", "opus"])
const VIDEO_EXT = new Set(["mp4", "mov", "avi", "mkv", "webm", "m4v"])
const SHEET_EXT = new Set(["xls", "xlsx", "csv", "tsv", "ods"])
const SLIDES_EXT = new Set(["ppt", "pptx", "odp"])
const DOC_EXT = new Set(["doc", "docx", "odt", "rtf"])
const TEXT_EXT = new Set(["txt", "md", "log", "json", "xml", "yaml", "yml"])

/** Extensión (lowercase, sin punto) de un nombre o extensión cruda. */
export function extOf(nameOrExt?: string | null): string {
  const s = (nameOrExt ?? "").toLowerCase().trim()
  if (!s) return ""
  return s.includes(".") ? s.slice(s.lastIndexOf(".") + 1) : s
}

/** Tipo de un adjunto a partir de su content-type y/o su nombre/extensión. */
export function attachmentKind(contentType?: string | null, nameOrExt?: string | null): AttachmentKind {
  const ct = (contentType ?? "").toLowerCase()
  const ext = extOf(nameOrExt)
  if (ct.includes("pdf") || ext === "pdf") return "pdf"
  if (ct.startsWith("image/") || IMAGE_EXT.has(ext)) return "image"
  if (ct.includes("zip") || ct.includes("compressed") || ct.includes("x-7z") || ct.includes("x-rar") || ARCHIVE_EXT.has(ext))
    return "zip"
  if (ct.startsWith("audio/") || AUDIO_EXT.has(ext)) return "audio"
  if (ct.startsWith("video/") || VIDEO_EXT.has(ext)) return "video"
  if (ct.includes("spreadsheet") || ct.includes("excel") || SHEET_EXT.has(ext)) return "sheet"
  if (ct.includes("presentation") || ct.includes("powerpoint") || SLIDES_EXT.has(ext)) return "slides"
  if (ct.includes("word") || ct === "application/msword" || ct.includes("opendocument.text") || DOC_EXT.has(ext))
    return "doc"
  if (ct.startsWith("text/") || TEXT_EXT.has(ext)) return "text"
  return "file"
}

/** Mapea el `media_kind` de chat/social (telegram/instagram/…) a un tipo de adjunto. */
export function mediaKindToAttachment(mediaKind?: string | null): AttachmentKind {
  switch ((mediaKind ?? "").toLowerCase()) {
    case "photo":
    case "image":
    case "carousel":
    case "sticker":
      return "image"
    case "video":
    case "reel":
      return "video"
    case "audio":
    case "voice":
      return "audio"
    default:
      return "file"
  }
}

/** Tipos únicos (orden estable) a partir de una lista de adjuntos. */
export function uniqueKinds(kinds: AttachmentKind[]): AttachmentKind[] {
  return [...new Set(kinds)]
}
