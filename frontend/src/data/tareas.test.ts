import { describe, expect, it } from "vitest"
import { groupTareas, toTarea, type Tarea } from "./tareas"

describe("toTarea", () => {
  it("mapea la fila del API y saca due_source/origin de metadata", () => {
    const t = toTarea({
      id: 51,
      title: "Ver el video y tomar notas",
      description: "",
      due_on: "2026-09-07",
      priority: "normal",
      status: "vencida",
      asunto_id: 157,
      asunto_name: "Intensivo de inglés B1.1",
      done_at: null,
      metadata: {
        due_kind: "proxima_sesion",
        due_source: "proxima_sesion",
        origin: "extraction",
        links: ["https://canva.link/x", 7],
      },
      created_at: "2026-09-13T06:22:00Z",
      updated_at: "2026-09-13T06:40:00Z",
      overdue: false,
    })
    expect(t.status).toBe("vencida")
    expect(t.dueOn).toBe("2026-09-07")
    expect(t.dueSource).toBe("proxima_sesion")
    expect(t.origin).toBe("extraction")
    expect(t.asuntoName).toBe("Intensivo de inglés B1.1")
    expect(t.links).toEqual(["https://canva.link/x"])
  })

  it("tolera metadata vacía", () => {
    const t = toTarea({
      id: 1,
      title: "x",
      description: "",
      due_on: null,
      priority: "alta",
      status: "pendiente",
      asunto_id: null,
      asunto_name: null,
      done_at: null,
      metadata: {},
      created_at: "2026-09-13T06:22:00Z",
      updated_at: "2026-09-13T06:22:00Z",
      overdue: false,
    })
    expect(t.dueSource).toBeNull()
    expect(t.priority).toBe("alta")
  })
})

function mk(id: number, parentId: number | null = null, status: Tarea["status"] = "pendiente"): Tarea {
  return {
    id,
    title: `t${id}`,
    description: "",
    dueOn: null,
    priority: "normal",
    category: "otro",
    status,
    asuntoId: 1,
    asuntoName: "x",
    doneAt: null,
    dueSource: null,
    origin: "extraction",
    discardReason: null,
    sourceInboxIds: [],
    parentId,
    links: [],
    isParent: parentId == null && id === 10,
    overdue: false,
    createdAt: "",
    updatedAt: "",
  }
}

describe("groupTareas", () => {
  it("cuelga los pasos de su proyecto y deja como raíz lo que no tiene padre presente", () => {
    const all = [mk(10), mk(11, 10), mk(12, 10, "vencida"), mk(20), mk(30, 99)]
    const { roots, childrenOf } = groupTareas(all)
    expect(roots.map((t) => t.id)).toEqual([10, 20, 30]) // 30: su padre no está en el conjunto
    expect((childrenOf.get(10) ?? []).map((t) => t.id)).toEqual([11, 12])
  })

  it("un paso cancelado o descartado no se anida: se lista aparte entre las cerradas", () => {
    const all = [mk(10), mk(11, 10), mk(12, 10, "cancelada")]
    const { roots, childrenOf } = groupTareas(all)
    expect(roots.map((t) => t.id)).toEqual([10, 12])
    expect((childrenOf.get(10) ?? []).map((t) => t.id)).toEqual([11])
  })

  it("con contexto, una sección que solo trae pasos no los repite como raíces", () => {
    const all = [mk(10), mk(11, 10), mk(12, 10, "vencida")]
    const vencidas = all.filter((t) => t.status === "vencida")
    const { roots } = groupTareas(vencidas, all)
    expect(roots).toEqual([]) // el paso vencido se ve bajo su proyecto, no suelto
  })
})
