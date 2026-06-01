// Formateo amigable del inbox para la vista /datos: etiqueta+icono de fuente legible (no el slug
// interno "imap-gmail-oauth"), separación asunto/snippet con texto limpio, y agrupado por día.

import { AtSign, CalendarDays, Mail, Send, Webhook, type LucideIcon } from "lucide-react"
import { attachmentKind, mediaKindToAttachment, uniqueKinds, type AttachmentKind } from "@/lib/attachment-kind"
import type { InboxRow, Source } from "@/types/domain"

function rec(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {}
}
function str(v: unknown): string {
  return typeof v === "string" ? v : ""
}

export interface SourceMeta {
  label: string
  icon: LucideIcon
  /** clase de color (token de dominio) para el icono/acento de la fuente. */
  tone: string
}

function titleize(s: string): string {
  const clean = s.replace(/[-_]+/g, " ").trim()
  return clean ? clean[0].toUpperCase() + clean.slice(1) : "Fuente"
}

/** Traduce una Source al par {etiqueta amigable, icono, acento} — nunca el nombre interno crudo. */
export function sourceMeta(source?: Source): SourceMeta {
  const cfg = rec(source?.config)
  const host = `${str(cfg.host)} ${str(cfg.server)}`.toLowerCase()
  const type = String(source?.type ?? "")
  if (type === "imap") {
    const label = host.includes("gmail")
      ? "Gmail"
      : host.includes("outlook") || host.includes("office365")
        ? "Outlook"
        : "Correo"
    return { label, icon: Mail, tone: "text-chart-1" }
  }
  if (type === "outlook") return { label: "Outlook", icon: Mail, tone: "text-chart-1" }
  if (type === "telegram") return { label: "Telegram", icon: Send, tone: "text-chart-2" }
  if (type === "social" || type === "instagram" || type === "facebook" || type === "x") {
    const p = str(cfg.platform) || type
    return { label: p[0].toUpperCase() + p.slice(1), icon: AtSign, tone: "text-chart-4" }
  }
  if (type === "calendar") return { label: "Calendario", icon: CalendarDays, tone: "text-chart-3" }
  return { label: titleize(source?.name ?? type), icon: Webhook, tone: "text-muted-foreground" }
}

/** Limpia un cuerpo crudo de correo para un snippet amigable: sin URLs, sin separadores ASCII,
 * whitespace colapsado. */
export function cleanText(s: string): string {
  return s
    .replace(/https?:\/\/\S+/g, "")
    .replace(/\(\s*\)/g, "")
    .replace(/[*=_~`-]{2,}/g, " ")
    .replace(/\s+/g, " ")
    .trim()
}

export type RowKind = "email" | "chat" | "social" | "other"

export interface RowSummary {
  kind: RowKind
  sender: string
  /** Email: asunto. Chat/social: el texto del mensaje (no hay asunto). */
  title: string
  /** Email: cuerpo limpio (cuando hay asunto). Chat/social: vacío. */
  snippet: string
  hasMedia: boolean
  /** Etiqueta de media para chat/social cuando no hay texto (p. ej. "foto"). */
  mediaLabel: string
  /** Tipos de adjunto ÚNICOS (para los íconos por fila). Email: por content_type/filename. */
  attachmentKinds: AttachmentKind[]
}

function rawAttachments(p: Record<string, unknown>): { filename: unknown; content_type: unknown }[] {
  return Array.isArray(p.attachments) ? (p.attachments as { filename: unknown; content_type: unknown }[]) : []
}

/** Saca {kind, remitente, título, snippet, media} del payload agnóstico, separando asunto de cuerpo. */
export function summarizeRow(row: InboxRow): RowSummary {
  const p = rec(row.payload)

  // Email: from + subject + body_text + attachments.
  if ("subject" in p || "body_text" in p || "folder" in p) {
    const from = rec(p.from)
    const sender = str(from.name) || str(from.email).split("@")[0] || "—"
    const subject = str(p.subject).trim()
    const body = cleanText(str(p.body_text))
    const attachments = rawAttachments(p)
    const attachmentKinds = uniqueKinds(
      attachments.map((a) => attachmentKind(str(a.content_type), str(a.filename))),
    )
    return {
      kind: "email",
      sender,
      title: subject || (body ? body.slice(0, 90) : "(sin asunto)"),
      snippet: subject ? body : "",
      hasMedia: attachments.length > 0,
      mediaLabel: "",
      attachmentKinds,
    }
  }

  // Telegram: chat/sender + text/caption.
  if ("chat_id" in p || "chat_kind" in p) {
    const sender =
      str(rec(p.sender).display_name) || str(rec(p.sender).username) || str(p.chat_title) || "Telegram"
    const text = cleanText(str(p.text) || str(p.media_caption))
    const media = str(p.media_kind)
    const hasMedia = !!media && media !== "none"
    return {
      kind: "chat",
      sender,
      title: text,
      snippet: "",
      hasMedia,
      mediaLabel: hasMedia ? media : "",
      attachmentKinds: hasMedia ? [mediaKindToAttachment(media)] : [],
    }
  }

  // Social: account + text.
  if ("post_id" in p || "platform" in p) {
    const sender = str(p.account_name) || str(p.account) || "Social"
    const text = cleanText(str(p.text))
    const media = str(p.media_kind)
    const hasMedia = !!media && media !== "none"
    return {
      kind: "social",
      sender,
      title: text,
      snippet: "",
      hasMedia,
      mediaLabel: hasMedia ? media : "",
      attachmentKinds: hasMedia ? [mediaKindToAttachment(media)] : [],
    }
  }

  // Fallback genérico.
  const sender = str(rec(p.from).email) || str(p.account) || "—"
  return {
    kind: "other",
    sender,
    title: cleanText(JSON.stringify(p)).slice(0, 90),
    snippet: "",
    hasMedia: false,
    mediaLabel: "",
    attachmentKinds: [],
  }
}

/** Iniciales para el avatar (1-2 letras). */
export function initials(name: string): string {
  const parts = name.replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return "?"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

const DAY_FMT = new Intl.DateTimeFormat("es", { weekday: "short", day: "2-digit", month: "short" })

/** Etiqueta de grupo por día: "Hoy" / "Ayer" / "lun 12 may". */
export function dayLabel(iso: string, now: Date): string {
  const d = new Date(iso)
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000)
  if (days <= 0) return "Hoy"
  if (days === 1) return "Ayer"
  return DAY_FMT.format(d)
}
