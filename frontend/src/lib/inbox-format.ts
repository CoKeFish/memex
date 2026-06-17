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
  /** Alias de la cuenta (p. ej. el email) para distinguir varias cuentas del mismo proveedor.
   * "" cuando no se puede derivar. */
  account: string
}

function titleize(s: string): string {
  const clean = s.replace(/[-_]+/g, " ").trim()
  return clean ? clean[0].toUpperCase() + clean.slice(1) : "Fuente"
}

/** Tokens de ruido en el nombre de la fuente que NO aportan al alias (protocolo, auth, genéricos). */
const ALIAS_NOISE = new Set([
  "imap", "imaps", "smtp", "pop", "pop3", "oauth", "oauth2", "basic", "auth", "ssl", "tls",
  "mail", "email", "correo", "account", "cuenta",
])

/** Deriva un alias de cuenta legible a partir del NOMBRE de la fuente (nunca el email ni el slug
 * crudo): quita ruido (imap/oauth/…) y el propio proveedor para no duplicar. El alias "real"
 * editable por el usuario vivirá en la vista Cuenta (pendiente, ver backlog). */
function accountAlias(source: Source | undefined, providerLabel: string): string {
  // El "·" entra como separador: hay nombres de fuente que ya traen "Proveedor · alias" y sin esto
  // el alias derivado duplica el punto medio ("Telegram · · personal").
  const tokens = String(source?.name ?? "").split(/[\s_\-./·]+/).filter(Boolean)
  const noise = new Set([...ALIAS_NOISE, providerLabel.toLowerCase()])
  const kept = tokens.filter((t) => !noise.has(t.toLowerCase()))
  if (kept.length === 0) return ""
  const alias = titleize(kept.join(" "))
  return alias.toLowerCase() === providerLabel.toLowerCase() ? "" : alias
}

function baseSourceMeta(source?: Source): Omit<SourceMeta, "account"> {
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

/** Traduce una Source a {etiqueta amigable de proveedor, icono, acento, alias de cuenta} — nunca
 * el nombre interno crudo. */
export function sourceMeta(source?: Source): SourceMeta {
  const base = baseSourceMeta(source)
  // Preferir la identidad REAL de la cuenta (alias que define el usuario → email real); caer a la
  // derivación del nombre solo si no hay ninguna. Así el rótulo dice de qué cuenta/buzón es.
  const real = (source?.accountAlias ?? "").trim() || (source?.accountEmail ?? "").trim()
  return { ...base, account: real || accountAlias(source, base.label) }
}

/** Etiqueta completa para selectores: "Gmail · roy@gmail.com" (proveedor · alias). Si no hay
 * alias, solo el proveedor. */
export function sourceFullLabel(source?: Source): string {
  const { label, account } = sourceMeta(source)
  return account ? `${label} · ${account}` : label
}

//: Etiquetas en español de los medios (los kinds del backend son email/chat/social).
export const KIND_LABELS: Record<string, string> = {
  email: "correo",
  chat: "chat",
  social: "social",
}

/** Etiqueta de un mensaje de origen por su medio: «correo #12» / «chat #12» / «social #12»;
 * «mensaje #12» si el medio no se conoce (fila borrada o tipo sin kind). */
export function inboxRefLabel(id: number, kinds: Record<number, string>): string {
  const k = kinds[id]
  return `${(k && KIND_LABELS[k]) || "mensaje"} #${id}`
}

/** Icono por medio (mismo mapeo que `baseSourceMeta`), con `unknown` como fallback. Mapa y no
 * función: el lint del React Compiler (static-components) trata el resultado de una llamada usado
 * como JSX como «componente creado en render»; el acceso por propiedad pasa. */
export const INBOX_KIND_ICON: Record<string, LucideIcon> = {
  email: Mail,
  chat: Send,
  social: AtSign,
  unknown: Webhook,
}

const KIND_ORDER = ["email", "chat", "social", "other"]

export interface SourceKindGroup {
  kind: string
  label: string
  sources: Source[]
}

/** Agrupa fuentes por su kind (server-driven) para selectores: correo/chat/social + «otras» para
 * tipos sin kind. Grupos vacíos se omiten; el orden interno respeta el de la API. */
export function groupSourcesByKind(sources: Source[]): SourceKindGroup[] {
  const by = new Map<string, Source[]>()
  for (const s of sources) {
    const k = s.kind ?? "other"
    const arr = by.get(k) ?? []
    arr.push(s)
    by.set(k, arr)
  }
  return KIND_ORDER.filter((k) => by.has(k)).map((k) => ({
    kind: k,
    label: KIND_LABELS[k] ?? "otras",
    sources: by.get(k) ?? [],
  }))
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
  /** Chat: el grupo/canal de origen (chat_title) cuando el remitente es una persona — una fuente
   * telegram mezcla varios chats y sin esto no se distingue de cuál viene cada mensaje. Vacío si
   * el remitente YA es el chat (canales sin sender) o para email/social. */
  context: string
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
      context: "",
      title: subject || (body ? body.slice(0, 90) : "(sin asunto)"),
      snippet: subject ? body : "",
      hasMedia: attachments.length > 0,
      mediaLabel: "",
      attachmentKinds,
    }
  }

  // Telegram: chat/sender + text/caption. `context` = el grupo, solo cuando el remitente es una
  // persona (si cae al fallback chat_title quedaría "Grupo X · Grupo X").
  if ("chat_id" in p || "chat_kind" in p) {
    const person = str(rec(p.sender).display_name) || str(rec(p.sender).username)
    const sender = person || str(p.chat_title) || "Telegram"
    const text = cleanText(str(p.text) || str(p.media_caption))
    const media = str(p.media_kind)
    const hasMedia = !!media && media !== "none"
    return {
      kind: "chat",
      sender,
      context: person ? str(p.chat_title) : "",
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
      context: "",
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
    context: "",
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
