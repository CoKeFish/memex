import { describe, expect, it } from "vitest"
import { groupSourcesByKind, summarizeRow } from "./inbox-format"
import type { InboxPayload, InboxRow, Source } from "../types/domain"

function rowWith(payload: Record<string, unknown>): InboxRow {
  return {
    id: 1,
    sourceId: 1,
    externalId: "x",
    occurredAt: "2026-06-01T12:00:00Z",
    receivedAt: "2026-06-01T12:00:00Z",
    payload: payload as unknown as InboxPayload,
    processedAt: null,
    processError: null,
    attempts: 0,
  }
}

describe("summarizeRow · context (grupo de origen del chat)", () => {
  it("telegram con persona + chat_title → context = el grupo", () => {
    const s = summarizeRow(
      rowWith({
        chat_id: 1,
        chat_kind: "group",
        chat_title: "Parche uni",
        sender: { user_id: 7, display_name: "Beto" },
        text: "nos vemos a las 6",
      }),
    )
    expect(s.kind).toBe("chat")
    expect(s.sender).toBe("Beto")
    expect(s.context).toBe("Parche uni")
    expect(s.title).toBe("nos vemos a las 6")
  })

  it("canal sin sender (remitente = chat_title) → context vacío, sin duplicar", () => {
    const s = summarizeRow(
      rowWith({ chat_id: 2, chat_kind: "channel", chat_title: "Noticias Dev", text: "post" }),
    )
    expect(s.sender).toBe("Noticias Dev")
    expect(s.context).toBe("")
  })

  it("email y social no llevan context", () => {
    const email = summarizeRow(
      rowWith({ subject: "Hola", body_text: "cuerpo", from: { name: "Ana", email: "a@b.c" } }),
    )
    expect(email.kind).toBe("email")
    expect(email.context).toBe("")
    // Regresión: asunto/snippet siguen separados como antes.
    expect(email.title).toBe("Hola")
    expect(email.snippet).toBe("cuerpo")

    const social = summarizeRow(
      rowWith({ platform: "x", account: "nasa", post_id: "1", text: "lanzamiento" }),
    )
    expect(social.kind).toBe("social")
    expect(social.context).toBe("")
    expect(social.sender).toBe("nasa")
  })
})

function srcWith(id: number, kind: Source["kind"]): Source {
  return {
    id,
    name: `s${id}`,
    type: "imap",
    enabled: true,
    createdAt: "2026-06-01T12:00:00Z",
    config: {},
    fetchModes: ["incremental"],
    kind,
  }
}

describe("groupSourcesByKind · agrupado del selector de fuente", () => {
  it("agrupa por medio en orden correo → chat → social", () => {
    const groups = groupSourcesByKind([
      srcWith(1, "social"),
      srcWith(2, "email"),
      srcWith(3, "chat"),
      srcWith(4, "email"),
    ])
    expect(groups.map((g) => g.label)).toEqual(["correo", "chat", "social"])
    expect(groups[0].sources.map((s) => s.id)).toEqual([2, 4])
  })

  it("kind null/ausente cae al grupo «otras», al final", () => {
    const groups = groupSourcesByKind([srcWith(1, null), srcWith(2, "email")])
    expect(groups.map((g) => g.kind)).toEqual(["email", "other"])
    expect(groups[1].label).toBe("otras")
    expect(groups[1].sources.map((s) => s.id)).toEqual([1])
  })

  it("omite grupos vacíos y devuelve [] sin fuentes", () => {
    expect(groupSourcesByKind([])).toEqual([])
    const groups = groupSourcesByKind([srcWith(1, "chat")])
    expect(groups).toHaveLength(1)
    expect(groups[0].kind).toBe("chat")
  })

  it("conserva el orden de la API dentro de cada grupo", () => {
    const groups = groupSourcesByKind([srcWith(9, "email"), srcWith(3, "email"), srcWith(5, "email")])
    expect(groups[0].sources.map((s) => s.id)).toEqual([9, 3, 5])
  })
})
