import { describe, expect, it } from "vitest"
import type { AsuntoEvidence, AsuntoHito } from "@/data/asuntos"
import type { Tarea } from "@/data/tareas"
import {
  agendaOf,
  datoShape,
  dateParts,
  documentosOf,
  formatBytes,
  groupHitosByMonth,
  hitosNewestFirst,
  hostOf,
  relativeDay,
  splitElenco,
  tareasSummary,
} from "./asunto-story"

function tarea(over: Partial<Tarea> & { id: number; title: string }): Tarea {
  return {
    description: "",
    dueOn: null,
    priority: "normal",
    category: "otro",
    status: "pendiente",
    asuntoId: 1,
    asuntoName: null,
    doneAt: null,
    dueSource: null,
    origin: "extraction",
    discardReason: null,
    sourceInboxIds: [],
    parentId: null,
    links: [],
    isParent: false,
    overdue: false,
    createdAt: "2026-09-01T00:00:00Z",
    updatedAt: "2026-09-01T00:00:00Z",
    ...over,
  }
}

describe("agendaOf", () => {
  const today = "2026-09-13"
  const tareas: Tarea[] = [
    tarea({ id: 1, title: "Proyecto de Instagram", isParent: true }),
    tarea({ id: 2, title: "Hacer el primer post", parentId: 1, dueOn: "2026-09-15", status: "completada" }),
    tarea({ id: 3, title: "Hacer el segundo post", parentId: 1, dueOn: "2026-09-23" }),
    tarea({ id: 4, title: "Paso cancelado", parentId: 1, dueOn: "2026-12-01", status: "cancelada" }),
    tarea({ id: 5, title: "Llevar papel", dueOn: "2026-09-10", dueSource: "proxima_sesion" }),
    tarea({ id: 6, title: "Leer capítulo", dueOn: "2026-09-14" }),
    tarea({ id: 7, title: "Sin fecha" }),
    tarea({ id: 8, title: "Ya vencida", dueOn: "2026-09-01", status: "vencida" }),
  ]

  it("ordena por fecha, marca lo atrasado y resume el proyecto por su entrega", () => {
    const a = agendaOf(tareas, today)
    expect(a.items.map((i) => i.id)).toEqual([5, 6, 1])
    expect(a.items[0]).toMatchObject({ late: true, kind: "tarea", detail: "para la próxima clase" })
    expect(a.items[1]).toMatchObject({ late: false, detail: null })
    // entrega = la mayor fecha entre los pasos vivos (el cancelado no cuenta); avance 1 de 2
    expect(a.items[2]).toMatchObject({ kind: "entrega", date: "2026-09-23", detail: "1 de 2 pasos" })
    expect(a.undated).toBe(1)
    expect(a.more).toBe(0)
  })

  it("aplica el tope y cuenta los que quedan fuera", () => {
    const a = agendaOf(tareas, today, 2)
    expect(a.items).toHaveLength(2)
    expect(a.more).toBe(1)
  })

  it("un proyecto sin fechas cuenta como sin fecha", () => {
    const a = agendaOf([tarea({ id: 1, title: "P", isParent: true }), tarea({ id: 2, title: "s", parentId: 1 })], today)
    expect(a.items).toEqual([])
    expect(a.undated).toBe(1)
  })
})

describe("datoShape / hostOf", () => {
  it("reconoce enlaces, códigos y texto", () => {
    expect(datoShape("https://campus.colombobogota.edu.co/")).toBe("url")
    expect(datoShape("QiwJN-zjIwXZ")).toBe("code")
    expect(datoShape("ana.perez@colombobogota.edu.co")).toBe("code")
    expect(datoShape("3001234567")).toBe("code")
    expect(datoShape("Salón 204, segundo piso")).toBe("text")
    expect(datoShape("lunes")).toBe("text")
  })
  it("acorta el enlace a host y ruta", () => {
    expect(hostOf("https://www.englishhub.oup.com/")).toBe("englishhub.oup.com")
    expect(hostOf("https://docs.google.com/document/d/abcdefghijklmnopqrstuvwxyz/edit")).toMatch(
      /^docs\.google\.com\/document\/d\/.*…$/,
    )
    expect(hostOf("no es url")).toBe("no es url")
  })
})

describe("fechas", () => {
  it("dateParts no corre el día por la zona horaria", () => {
    expect(dateParts("2026-09-17")).toMatchObject({ day: 17, month: "sep", year: 2026 })
  })
  it("relativeDay", () => {
    expect(relativeDay("2026-09-13", "2026-09-13")).toBe("hoy")
    expect(relativeDay("2026-09-14", "2026-09-13")).toBe("mañana")
    expect(relativeDay("2026-09-16", "2026-09-13")).toBe("en 3 días")
    expect(relativeDay("2026-09-11", "2026-09-13")).toBe("hace 2 días")
    expect(relativeDay("2026-09-23", "2026-09-13")).toMatch(/^el .* 23 sep$/)
  })
})

describe("cronología", () => {
  const hitos: AsuntoHito[] = [
    { date: "2026-08-31", dateIsExact: true, text: "a", inboxId: 1, evidenceId: 1, notaId: null },
    { date: null, dateIsExact: false, text: "b", inboxId: 2, evidenceId: 2, notaId: null },
    { date: "2026-09-09", dateIsExact: true, text: "c", inboxId: 3, evidenceId: 3, notaId: null },
  ]
  it("más reciente primero, sin fecha al final, agrupado por mes", () => {
    const sorted = hitosNewestFirst(hitos)
    expect(sorted.map((h) => h.text)).toEqual(["c", "a", "b"])
    const groups = groupHitosByMonth(sorted)
    expect(groups.map((g) => g.key)).toEqual(["2026-09", "2026-08", "sin-fecha"])
  })
})

describe("evidencia y elenco", () => {
  it("documentosOf filtra el medio documentos y ordena por fecha", () => {
    const ev = (id: number, kind: string | null, at: string): AsuntoEvidence => ({
      id,
      inboxId: id,
      kind,
      method: "manual",
      confidence: 1,
      occurredAt: at,
      subject: `f${id}.pdf`,
      sender: "",
      hitoText: null,
      hitoDate: null,
      filename: null,
      mediaId: null,
      sizeBytes: null,
    })
    const docs = documentosOf([ev(1, "documentos", "2026-09-01"), ev(2, "chat", "2026-09-05"), ev(3, "documentos", "2026-09-03")])
    expect(docs.map((d) => d.id)).toEqual([3, 1])
  })
  it("splitElenco separa personas y organizaciones del resto", () => {
    const items = [
      { slug: "persona", id: 1, label: "Ana", kind: "persona", messages: 3, edgeId: 1 },
      { slug: "canal", id: 2, label: "Grupo", kind: "canal", messages: 9, edgeId: 2 },
      { slug: "organizacion", id: 3, label: "Britano", kind: "organizacion", messages: 1, edgeId: 3 },
    ]
    const { people, rest } = splitElenco(items)
    expect(people.map((p) => p.label)).toEqual(["Ana", "Britano"])
    expect(rest.map((p) => p.label)).toEqual(["Grupo"])
  })
  it("tareasSummary", () => {
    expect(
      tareasSummary([
        tarea({ id: 1, title: "p", isParent: true }),
        tarea({ id: 2, title: "a" }),
        tarea({ id: 3, title: "b", status: "vencida" }),
        tarea({ id: 4, title: "c", status: "completada" }),
      ]),
    ).toBe("1 pendiente · 1 proyecto · 1 vencida · 1 cerrada")
  })
})

describe("formatBytes", () => {
  it("elige la unidad y usa coma decimal", () => {
    expect(formatBytes(12)).toBe("12 B")
    expect(formatBytes(28449)).toBe("28 KB")
    expect(formatBytes(2965125)).toBe("2,8 MB")
    expect(formatBytes(67855266)).toBe("65 MB")
    expect(formatBytes(null)).toBe("")
  })
})
