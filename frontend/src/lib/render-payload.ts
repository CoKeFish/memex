// Port TS de `memex.processing.render.render_payload`: arma `{sender}: {texto}` a
// partir de un inbox.payload agnóstico de fuente, probando las claves de email /
// telegram / social. Mismo orden de precedencia que el Python para que el feed de
// inbox muestre lo mismo que ve el summarizer/los módulos. La paridad (incluido el
// manifest de adjuntos y su formato de tamaño) se fija con vectores de test idénticos
// en `render-payload.test.ts` y `tests/test_processing_render.py`.

export interface RenderedPayload {
  sender: string
  body: string
  /** `${sender}: ${body}` cuando hay remitente; si no, solo el cuerpo. */
  line: string
}

type Payload = Record<string, unknown>

function asRecord(v: unknown): Payload | null {
  return v && typeof v === "object" ? (v as Payload) : null
}

/** Tamaño legible en base 1000 con aritmética ENTERA — espejo exacto de `_format_size` (Python).
 * Nada de `Math.round`/`toFixed`: divergen del `(n + mitad) // unidad` del lado Python. */
function formatSize(n: number): string {
  if (n >= 1_000_000) {
    const tenths = Math.floor((n + 50_000) / 100_000)
    return `${Math.floor(tenths / 10)}.${tenths % 10} MB`
  }
  if (n >= 1_000) return `${Math.floor((n + 500) / 1_000)} KB`
  return `${n} B`
}

/** `[Adjuntos: …]` desde los declarados, o "" — espejo exacto de `_attachments_manifest`. */
function attachmentsManifest(payload: Payload): string {
  const atts = payload.attachments
  if (!Array.isArray(atts)) return ""
  const items: string[] = []
  for (const raw of atts) {
    const a = asRecord(raw)
    if (!a || Array.isArray(raw)) continue // espejo de `isinstance(raw, dict)`
    const name = String(a.filename || a.content_type || "adjunto")
    const size = typeof a.size === "number" && a.size > 0 ? Math.floor(a.size) : 0
    items.push(size > 0 ? `${name} (${formatSize(size)})` : name)
  }
  if (items.length === 0) return ""
  return `[Adjuntos: ${items.join(", ")}]`
}

export function renderPayload(input: unknown, ocrText = ""): RenderedPayload {
  const payload: Payload = asRecord(input) ?? {}
  let sender = ""
  const frm = asRecord(payload.from)
  if (frm) sender = String(frm.name ?? frm.email ?? "")
  const snd = asRecord(payload.sender)
  if (!sender && snd) sender = String(snd.display_name ?? snd.username ?? "")
  if (!sender) sender = String(payload.account ?? payload.chat_title ?? "")

  const parts: string[] = []
  const subject = payload.subject
  if (subject) parts.push(`Asunto: ${String(subject)}`)
  const body = (payload.body_text || payload.text || payload.media_caption || "") as string
  if (body) parts.push(String(body))
  const manifest = attachmentsManifest(payload)
  if (manifest) parts.push(manifest)
  if (ocrText.trim()) parts.push(`[Texto en imágenes adjuntas]:\n${ocrText.trim()}`)

  const text = parts.join("\n").trim()
  return { sender, body: text, line: sender ? `${sender}: ${text}` : text }
}
