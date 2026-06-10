import { describe, expect, it } from "vitest"
import { FILTER_PAYLOAD_DOCS, OPERATOR_DOCS } from "./filter-attributes"

// VECTORES ESPEJO de tests/test_filter_attributes_parity.py (paths derivados de los modelos
// Pydantic de core/payloads.py): no hay runner cross-language, la paridad se fija duplicando
// estos vectores — cambiar uno = cambiar el otro.
const EMAIL_PATHS = [
  "attachments",
  "auto_submitted",
  "body_source",
  "body_text",
  "body_truncated",
  "cc",
  "date",
  "flags",
  "folder",
  "from.email",
  "from.name",
  "in_reply_to",
  "list_id",
  "list_unsubscribe",
  "list_unsubscribe_post",
  "message_id",
  "precedence",
  "raw_headers",
  "references",
  "reply_to",
  "size_bytes",
  "subject",
  "to",
]

const TELEGRAM_PATHS = [
  "chat_id",
  "chat_kind",
  "chat_title",
  "date",
  "forwarded_from",
  "media_caption",
  "media_kind",
  "message_id",
  "reply_to_message_id",
  "sender.display_name",
  "sender.is_bot",
  "sender.user_id",
  "sender.username",
  "text",
  "topic_id",
]

const SOCIAL_PATHS = [
  "account",
  "account_name",
  "engagement.comments",
  "engagement.likes",
  "engagement.shares",
  "engagement.views",
  "is_paid_partnership",
  "media_kind",
  "media_refs",
  "platform",
  "post_id",
  "posted_at",
  "raw_type",
  "shortcode",
  "text",
  "url",
]

const MIRROR: Record<string, string[]> = {
  email: EMAIL_PATHS,
  telegram: TELEGRAM_PATHS,
  social: SOCIAL_PATHS,
}

const OPS = new Set(["equals", "in", "regex", "prefix"])

function docFor(kind: string) {
  const doc = FILTER_PAYLOAD_DOCS.find((d) => d.kind === kind)
  if (!doc) throw new Error(`payload doc faltante: ${kind}`)
  return doc
}

describe("FILTER_PAYLOAD_DOCS (paridad con core/payloads.py)", () => {
  it("documenta exactamente los paths de los payload models (vector espejo)", () => {
    for (const [kind, expected] of Object.entries(MIRROR)) {
      const paths = docFor(kind)
        .attributes.map((a) => a.path)
        .sort()
      expect(paths, kind).toEqual(expected)
    }
  })

  it("paths únicos por payload", () => {
    for (const doc of FILTER_PAYLOAD_DOCS) {
      const paths = doc.attributes.map((a) => a.path)
      expect(new Set(paths).size, doc.kind).toBe(paths.length)
    }
  })

  it("cada example es un scope válido: JSON objeto, keys documentadas, UN operador conocido", () => {
    for (const doc of FILTER_PAYLOAD_DOCS) {
      const known = doc.attributes.filter((a) => a.matchable).map((a) => a.path)
      for (const attr of doc.attributes) {
        if (!attr.example) continue
        expect(attr.matchable, `${doc.kind}/${attr.path}: example sobre no-matcheable`).toBe(true)
        const scope = JSON.parse(attr.example) as Record<string, Record<string, unknown>>
        expect(typeof scope, attr.path).toBe("object")
        for (const [key, spec] of Object.entries(scope)) {
          // La key del scope debe ser un path documentado o un sub-path de un objeto documentado
          // (p. ej. raw_headers.X-Mailer bajo raw_headers).
          const ok = known.some((p) => key === p || key.startsWith(`${p}.`))
          expect(ok, `${doc.kind}/${attr.path}: key ${key} no documentada`).toBe(true)
          const ops = Object.keys(spec)
          expect(ops, `${doc.kind}/${attr.path}`).toHaveLength(1)
          expect(OPS.has(ops[0]), `${doc.kind}/${attr.path}: operador ${ops[0]}`).toBe(true)
        }
      }
    }
  })

  it("sourceTypes no vacíos y sin repetir entre payloads", () => {
    const all = FILTER_PAYLOAD_DOCS.flatMap((d) => d.sourceTypes)
    expect(all.length).toBeGreaterThan(0)
    expect(new Set(all).size).toBe(all.length)
    for (const doc of FILTER_PAYLOAD_DOCS) expect(doc.sourceTypes.length, doc.kind).toBeGreaterThan(0)
  })
})

describe("OPERATOR_DOCS", () => {
  it("los 4 operadores del DSL, cada example usa su propio operador", () => {
    expect(OPERATOR_DOCS.map((o) => o.op).sort()).toEqual(["equals", "in", "prefix", "regex"])
    for (const o of OPERATOR_DOCS) {
      const scope = JSON.parse(o.example) as Record<string, Record<string, unknown>>
      for (const spec of Object.values(scope)) {
        expect(Object.keys(spec), o.op).toEqual([o.op])
      }
    }
  })
})
