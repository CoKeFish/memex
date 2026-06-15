// Referencia curada de los atributos filtrables por el scope de filter_rules, espejo estático de
// los payload models de src/memex/core/payloads.py (EmailPayload / TelegramPayload /
// SocialPostPayload). La paridad la vigila tests/test_filter_attributes_parity.py (backend): si un
// campo cambia allá, ese test rompe y esta tabla se actualiza a mano. Convención "VECTORES ESPEJO"
// (misma que render-payload.ts ↔ tests/test_processing_render.py).

export interface OperatorDoc {
  op: "equals" | "in" | "regex" | "prefix"
  /** Forma del operador dentro del scope JSON. */
  signature: string
  description: string
  example: string
}

export const OPERATOR_DOCS: OperatorDoc[] = [
  {
    op: "equals",
    signature: '{"campo": {"equals": valor}}',
    description: "Igualdad estricta. Sirve para strings, números y booleanos.",
    example: '{"from.email": {"equals": "spam@x.com"}}',
  },
  {
    op: "in",
    signature: '{"campo": {"in": [v1, v2]}}',
    description: "El valor está en la lista. Strings, números o booleanos.",
    example: '{"chat_id": {"in": [-100123, -100456]}}',
  },
  {
    op: "regex",
    signature: '{"campo": {"regex": "patrón"}}',
    description: "Expresión regular de Python (re.search, no anclada). Solo strings.",
    example: '{"subject": {"regex": "(?i)factura|recibo"}}',
  },
  {
    op: "prefix",
    signature: '{"campo": {"prefix": "texto"}}',
    description: "El string empieza con el texto dado. Solo strings.",
    example: '{"subject": {"prefix": "[NEWSLETTER]"}}',
  },
]

export interface FilterAttribute {
  /** Path dot-notation desde la raíz del payload (lo que va como key del scope). */
  path: string
  /** Tipo para mostrar (refleja el modelo Pydantic). */
  type: string
  /** Descripción breve en español. */
  description: string
  /** Scope JSON completo de ejemplo, copiable. Solo en atributos matcheables. */
  example?: string
  /** false = array u objeto compuesto: el DSL v1 no matchea por elemento. */
  matchable: boolean
}

export interface PayloadDoc {
  kind: "email" | "telegram" | "social"
  /** Título del tab. */
  label: string
  /** Valores de sources.type que producen este payload (para el campo source_type de la regla). */
  sourceTypes: string[]
  /** Caveats propios del payload. */
  notes: string[]
  attributes: FilterAttribute[]
}

export const FILTER_PAYLOAD_DOCS: PayloadDoc[] = [
  {
    kind: "email",
    label: "Correo",
    sourceTypes: ["imap", "outlook"],
    notes: [
      "from es un objeto {email, name}: filtrá por from.email o from.name, no por from entero.",
      "raw_headers es un dict con headers whitelisted (X-Mailer, Received-SPF, Authentication-Results); se accede con dot-notation: raw_headers.X-Mailer.",
    ],
    attributes: [
      {
        path: "from.email",
        type: "string",
        description: "Email del remitente.",
        example: '{"from.email": {"equals": "promos@tienda.com"}}',
        matchable: true,
      },
      {
        path: "from.name",
        type: "string | null",
        description: "Nombre visible del remitente.",
        example: '{"from.name": {"regex": "(?i)newsletter"}}',
        matchable: true,
      },
      { path: "to", type: "array de {email, name}", description: "Destinatarios.", matchable: false },
      { path: "cc", type: "array de {email, name}", description: "Copia.", matchable: false },
      { path: "reply_to", type: "array de {email, name}", description: "Reply-To.", matchable: false },
      {
        path: "subject",
        type: "string | null",
        description: "Asunto.",
        example: '{"subject": {"prefix": "[NEWSLETTER]"}}',
        matchable: true,
      },
      {
        path: "date",
        type: "datetime (string ISO)",
        description: "Fecha del correo (header Date, o INTERNALDATE si falta/es absurda).",
        example: '{"date": {"prefix": "2026-06"}}',
        matchable: true,
      },
      {
        path: "message_id",
        type: "string | null",
        description: "Message-ID sin ángulos.",
        matchable: true,
      },
      {
        path: "in_reply_to",
        type: "string | null",
        description: "Message-ID al que responde.",
        matchable: true,
      },
      { path: "references", type: "array de string", description: "Cadena del hilo (Message-IDs).", matchable: false },
      {
        path: "list_id",
        type: "string | null",
        description: "Header List-ID. Su sola presencia manda el correo a Lista negra (heurística).",
        example: '{"list_id": {"regex": "ofertas"}}',
        matchable: true,
      },
      {
        path: "list_unsubscribe",
        type: "string | null",
        description: "Header List-Unsubscribe (bulk con opt-out). Presente → Lista negra (heurística).",
        matchable: true,
      },
      {
        path: "list_unsubscribe_post",
        type: "string | null",
        description: "Header List-Unsubscribe-Post (one-click).",
        matchable: true,
      },
      {
        path: "precedence",
        type: "string | null",
        description: "Header Precedence. bulk / list / junk → Lista negra (heurística).",
        example: '{"precedence": {"in": ["bulk", "junk"]}}',
        matchable: true,
      },
      {
        path: "auto_submitted",
        type: "string | null",
        description: 'Header Auto-Submitted. Distinto de "no" → Lista negra (heurística).',
        matchable: true,
      },
      {
        path: "body_text",
        type: "string",
        description: "Cuerpo en texto plano (o HTML convertido a texto).",
        example: '{"body_text": {"regex": "(?i)unsubscribe"}}',
        matchable: true,
      },
      {
        path: "body_source",
        type: '"text" | "html_stripped"',
        description: "De dónde salió el cuerpo.",
        matchable: true,
      },
      {
        path: "body_truncated",
        type: "boolean",
        description: "true si el cuerpo se cortó por tamaño.",
        matchable: true,
      },
      {
        path: "folder",
        type: "string",
        description: "Carpeta IMAP de origen.",
        example: '{"folder": {"equals": "INBOX"}}',
        matchable: true,
      },
      { path: "flags", type: "array de string", description: "Flags IMAP (\\Seen, \\Flagged…).", matchable: false },
      {
        path: "size_bytes",
        type: "number",
        description: "Tamaño del correo en bytes (equals/in; regex y prefix no aplican a números).",
        matchable: true,
      },
      {
        path: "attachments",
        type: "array de {filename, content_type, size, content_id}",
        description: "Metadata de adjuntos (sin contenido).",
        matchable: false,
      },
      {
        path: "raw_headers",
        type: "objeto {header: valor}",
        description: "Headers crudos whitelisted; accesibles por sub-path.",
        example: '{"raw_headers.X-Mailer": {"prefix": "Outlook"}}',
        matchable: true,
      },
    ],
  },
  {
    kind: "telegram",
    label: "Telegram",
    sourceTypes: ["telegram"],
    notes: [
      "sender puede ser null (service messages, posts anónimos de canal): un path sender.* que no resuelve hace que esa key no matchee.",
      "chat_id usa el formato marcado de Telethon: negativo para grupos y canales.",
    ],
    attributes: [
      {
        path: "chat_id",
        type: "number",
        description: "ID del chat (marked format; negativo en grupos/canales).",
        example: '{"chat_id": {"in": [-1001234567890]}}',
        matchable: true,
      },
      {
        path: "chat_kind",
        type: '"group" | "supergroup" | "channel"',
        description: "Tipo de chat (DMs no se ingestan).",
        example: '{"chat_kind": {"equals": "channel"}}',
        matchable: true,
      },
      { path: "chat_title", type: "string | null", description: "Título del chat.", matchable: true },
      {
        path: "topic_id",
        type: "number | null",
        description: "Topic del foro (root del topic); solo supergrupos con topics.",
        matchable: true,
      },
      { path: "message_id", type: "number", description: "ID del mensaje dentro del chat.", matchable: true },
      {
        path: "sender.user_id",
        type: "number",
        description: "ID de quien envió.",
        matchable: true,
      },
      {
        path: "sender.username",
        type: "string | null",
        description: "Username de quien envió (sin @).",
        example: '{"sender.username": {"equals": "spambot123"}}',
        matchable: true,
      },
      {
        path: "sender.display_name",
        type: "string | null",
        description: "Nombre visible (first+last o título).",
        matchable: true,
      },
      {
        path: "sender.is_bot",
        type: "boolean",
        description: "true si lo envió un bot.",
        example: '{"sender.is_bot": {"equals": true}}',
        matchable: true,
      },
      { path: "date", type: "datetime (string ISO)", description: "Fecha del mensaje.", matchable: true },
      {
        path: "text",
        type: "string",
        description: "Texto del mensaje.",
        example: '{"text": {"regex": "(?i)airdrop|gana dinero"}}',
        matchable: true,
      },
      {
        path: "reply_to_message_id",
        type: "number | null",
        description: "Mensaje al que responde.",
        matchable: true,
      },
      {
        path: "forwarded_from",
        type: "string | null",
        description: "Origen del forward (si es reenviado).",
        matchable: true,
      },
      {
        path: "media_kind",
        type: '"none" | "photo" | "video" | "document" | "audio" | "voice" | "sticker" | "other"',
        description: "Tipo de media adjunta.",
        example: '{"media_kind": {"equals": "sticker"}}',
        matchable: true,
      },
      { path: "media_caption", type: "string | null", description: "Caption de la media.", matchable: true },
    ],
  },
  {
    kind: "social",
    label: "Redes",
    sourceTypes: ["instagram", "facebook", "x"],
    notes: [
      "engagement puede ser null (el scraper no siempre lo devuelve): engagement.* sin resolver no matchea.",
      "account es SIEMPRE el handle de la allowlist que se pidió scrapear (no el owner crudo).",
    ],
    attributes: [
      {
        path: "platform",
        type: '"instagram" | "facebook" | "x"',
        description: "Red del post.",
        example: '{"platform": {"equals": "x"}}',
        matchable: true,
      },
      {
        path: "account",
        type: "string",
        description: "Handle de la allowlist que se scrapeó.",
        example: '{"account": {"equals": "alcaldiabogota"}}',
        matchable: true,
      },
      { path: "account_name", type: "string | null", description: "Nombre visible de la cuenta.", matchable: true },
      { path: "post_id", type: "string", description: "ID del post en la plataforma.", matchable: true },
      { path: "shortcode", type: "string | null", description: "Shortcode (Instagram).", matchable: true },
      { path: "url", type: "string", description: "URL del post.", matchable: true },
      {
        path: "text",
        type: "string",
        description: "Texto / caption del post.",
        example: '{"text": {"regex": "(?i)sorteo|giveaway"}}',
        matchable: true,
      },
      { path: "posted_at", type: "datetime (string ISO)", description: "Fecha de publicación.", matchable: true },
      {
        path: "media_kind",
        type: '"none" | "image" | "video" | "carousel" | "reel" | "other"',
        description: "Tipo de media del post.",
        example: '{"media_kind": {"equals": "reel"}}',
        matchable: true,
      },
      {
        path: "media_refs",
        type: "array de {url, kind, content_type}",
        description: "Referencias a la media (URLs de CDN, expiran).",
        matchable: false,
      },
      { path: "engagement.likes", type: "number | null", description: "Likes.", matchable: true },
      { path: "engagement.comments", type: "number | null", description: "Comentarios / replies.", matchable: true },
      { path: "engagement.shares", type: "number | null", description: "Shares / retweets.", matchable: true },
      { path: "engagement.views", type: "number | null", description: "Vistas.", matchable: true },
      {
        path: "is_paid_partnership",
        type: "boolean | null",
        description: "Post marcado como colaboración paga.",
        example: '{"is_paid_partnership": {"equals": true}}',
        matchable: true,
      },
      { path: "raw_type", type: "string | null", description: "Tipo crudo que reportó el scraper.", matchable: true },
    ],
  },
]
