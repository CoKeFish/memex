// Port TS de `memex.processing.render.render_payload`: arma `{sender}: {texto}` a
// partir de un inbox.payload agnóstico de fuente, probando las claves de email /
// telegram / social. Mismo orden de precedencia que el Python para que el feed de
// inbox muestre lo mismo que ve el summarizer/los módulos.

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
  if (ocrText.trim()) parts.push(`[Texto en imágenes adjuntas]:\n${ocrText.trim()}`)

  const text = parts.join("\n").trim()
  return { sender, body: text, line: sender ? `${sender}: ${text}` : text }
}
