import { describe, expect, it } from "vitest"
import { agruparTareas, bucketDeFecha, fechaDeOrden } from "./tareas-group"
import type { Tarea } from "@/data"

const HOY = "2026-09-15"
const AHORA = new Date(`${HOY}T12:00:00`)

let seq = 0
function t(over: Partial<Tarea> = {}): Tarea {
  seq += 1
  return {
    id: over.id ?? seq,
    title: `t${seq}`,
    description: "",
    dueOn: null,
    priority: "normal",
    category: "otro",
    status: "pendiente",
    asuntoId: null,
    asuntoName: null,
    doneAt: null,
    dueSource: null,
    origin: "manual",
    discardReason: null,
    sourceInboxIds: [],
    parentId: null,
    links: [],
    isParent: false,
    overdue: false,
    createdAt: "2026-09-01T00:00:00Z",
    updatedAt: "2026-09-01T00:00:00Z",
    ...over,
  } as Tarea
}

const sinPasos = (): Tarea[] => []

describe("fechaDeOrden", () => {
  it("un proyecto entra por su ENTREGA, no por su primer paso", () => {
    // Si ordenara por el primer paso, un proyecto de tres meses caería junto a lo de mañana.
    const proyecto = t({ isParent: true })
    const pasos = [t({ dueOn: "2026-09-16" }), t({ dueOn: "2026-12-01" })]
    expect(fechaDeOrden(proyecto, pasos)).toBe("2026-12-01")
  })

  it("los pasos ya cerrados no corren la entrega", () => {
    const proyecto = t({ isParent: true })
    const pasos = [t({ dueOn: "2026-09-16" }), t({ dueOn: "2026-12-01", status: "cancelada" })]
    expect(fechaDeOrden(proyecto, pasos)).toBe("2026-09-16")
  })

  it("sin ninguna fecha es null, no una fecha lejana", () => {
    expect(fechaDeOrden(t(), [])).toBeNull()
  })
})

describe("bucketDeFecha", () => {
  it("reparte por cercanía y deja lo sin fecha aparte", () => {
    expect(bucketDeFecha("2026-09-03", HOY)).toBe("atrasadas")
    expect(bucketDeFecha(HOY, HOY)).toBe("hoy")
    expect(bucketDeFecha("2026-09-20", HOY)).toBe("semana")
    expect(bucketDeFecha("2026-10-30", HOY)).toBe("adelante")
    expect(bucketDeFecha(null, HOY)).toBe("sin-fecha")
  })
})

describe("agruparTareas por cuándo", () => {
  it("ordena los grupos de lo más urgente a lo que no tiene fecha", () => {
    const roots = [
      t({ dueOn: "2026-10-30" }),
      t({ dueOn: null }),
      t({ dueOn: "2026-09-03" }),
      t({ dueOn: HOY }),
      t({ dueOn: "2026-09-18" }),
    ]
    const g = agruparTareas(roots, sinPasos, { groupBy: "cuando", sortBy: "fecha", now: AHORA })
    expect(g.map((x) => x.key)).toEqual(["atrasadas", "hoy", "semana", "adelante", "sin-fecha"])
    expect(g[0].urgent).toBe(true)
    expect(g[3].urgent).toBeUndefined()
  })

  it("un grupo vacío no aparece", () => {
    const g = agruparTareas([t({ dueOn: null })], sinPasos, {
      groupBy: "cuando",
      sortBy: "fecha",
      now: AHORA,
    })
    expect(g.map((x) => x.key)).toEqual(["sin-fecha"])
  })

  it("el proyecto cae en el grupo de su entrega", () => {
    const proyecto = t({ isParent: true })
    const pasos = [t({ dueOn: "2026-09-16", parentId: proyecto.id })]
    const g = agruparTareas([proyecto], () => pasos, {
      groupBy: "cuando",
      sortBy: "fecha",
      now: AHORA,
    })
    expect(g.map((x) => x.key)).toEqual(["semana"])
  })
})

describe("agruparTareas por asunto", () => {
  it("junta cada asunto y manda «sin asunto» al final", () => {
    const roots = [
      t({ asuntoId: null, dueOn: "2026-09-16" }),
      t({ asuntoId: 7, asuntoName: "Inglés B1.1", dueOn: "2026-09-20" }),
      t({ asuntoId: 3, asuntoName: "Hack Sirius", dueOn: "2026-09-17" }),
      t({ asuntoId: 7, asuntoName: "Inglés B1.1", dueOn: "2026-09-23" }),
    ]
    const g = agruparTareas(roots, sinPasos, { groupBy: "asunto", sortBy: "fecha", now: AHORA })
    expect(g.map((x) => x.label)).toEqual(["Hack Sirius", "Inglés B1.1", "Sin asunto"])
    expect(g[1].tareas).toHaveLength(2)
  })
})

describe("orden dentro del grupo", () => {
  it("por fecha: lo más próximo primero y lo sin fecha al final", () => {
    const roots = [t({ dueOn: null }), t({ dueOn: "2026-09-25" }), t({ dueOn: "2026-09-18" })]
    const [g] = agruparTareas(roots, sinPasos, { groupBy: "nada", sortBy: "fecha", now: AHORA })
    expect(g.tareas.map((x) => x.dueOn)).toEqual(["2026-09-18", "2026-09-25", null])
  })

  it("por prioridad: la alta primero aunque venza después", () => {
    const roots = [
      t({ dueOn: "2026-09-18", priority: "baja" }),
      t({ dueOn: "2026-09-25", priority: "alta" }),
    ]
    const [g] = agruparTareas(roots, sinPasos, { groupBy: "nada", sortBy: "prioridad", now: AHORA })
    expect(g.tareas.map((x) => x.priority)).toEqual(["alta", "baja"])
  })

  it("con la misma fecha desempata la prioridad", () => {
    const roots = [
      t({ dueOn: "2026-09-18", priority: "baja" }),
      t({ dueOn: "2026-09-18", priority: "alta" }),
    ]
    const [g] = agruparTareas(roots, sinPasos, { groupBy: "nada", sortBy: "fecha", now: AHORA })
    expect(g.tareas.map((x) => x.priority)).toEqual(["alta", "baja"])
  })
})
