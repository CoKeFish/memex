import { describe, expect, it } from "vitest"
import type { GraphEdge, GraphNode } from "@/data/graph"
import { collapseClusters } from "./graph-collapse"
import { nodeKey } from "./graph-layout"

function node(slug: string, id: number, kind = "persona"): GraphNode {
  return { slug, id, label: `${slug}#${id}`, kind, sourceInboxIds: [] }
}

function edge(id: number, a: GraphNode, b: GraphNode, over: Partial<GraphEdge> = {}): GraphEdge {
  return {
    id,
    srcSlug: a.slug,
    srcId: a.id,
    dstSlug: b.slug,
    dstId: b.id,
    relationType: "co-ocurrencia",
    producer: "inbox",
    status: "pista",
    confidence: null,
    evidence: "",
    ...over,
  }
}

const miembro = (id: number, m: GraphNode, c: GraphNode): GraphEdge =>
  edge(id, m, c, { relationType: "miembro_de", producer: "llm", status: "confirmed" })

const keys = (nodes: GraphNode[]): string[] => nodes.map((n) => nodeKey(n.slug, n.id))

describe("collapseClusters", () => {
  const cumulo = node("cumulo", 10, "cumulo")
  const m1 = node("identidades:person", 1)
  const m2 = node("finance", 2, "transaccion")
  const fuera = node("identidades:org", 3, "organizacion")

  it("plegado: miembros ocultos, cúmulo visible, miembro_de internos descartados", () => {
    const out = collapseClusters(
      [cumulo, m1, m2],
      [miembro(1, m1, cumulo), miembro(2, m2, cumulo), edge(3, m1, m2)],
      new Set(),
    )
    expect(keys(out.nodes)).toEqual([nodeKey("cumulo", 10)])
    expect(out.edges).toHaveLength(0) // todo era interno al cúmulo
  })

  it("re-rutea miembro↔exterior al cúmulo y agrega con contador", () => {
    const out = collapseClusters(
      [cumulo, m1, m2, fuera],
      [miembro(1, m1, cumulo), miembro(2, m2, cumulo), edge(3, m1, fuera), edge(4, m2, fuera)],
      new Set(),
    )
    expect(keys(out.nodes).sort()).toEqual([nodeKey("cumulo", 10), nodeKey("identidades:org", 3)].sort())
    expect(out.edges).toHaveLength(1)
    const e = out.edges[0]
    expect(e.relationType).toBe("agregada")
    expect(e.aggregateCount).toBe(2)
    expect(e.id).toBeLessThan(0)
  })

  it("expandido = identidad de paso", () => {
    const nodes = [cumulo, m1, m2, fuera]
    const edges = [miembro(1, m1, cumulo), miembro(2, m2, cumulo), edge(3, m1, fuera)]
    const out = collapseClusters(nodes, edges, new Set([10]))
    expect(out.nodes).toEqual(nodes)
    expect(out.edges).toEqual(edges)
  })

  it("anidado: el ancestro plegado MÁS EXTERNO domina", () => {
    // A (cúmulo 20) es miembro de B (cúmulo 30); x es miembro de A.
    const a = node("cumulo", 20, "cumulo")
    const b = node("cumulo", 30, "cumulo")
    const x = node("identidades:person", 4)
    const nodes = [a, b, x, fuera]
    const edges = [miembro(1, a, b), miembro(2, x, a), edge(3, x, fuera)]
    // B plegado → todo (incl. A y x) colapsa a B aunque A esté "expandido".
    const plegadoB = collapseClusters(nodes, edges, new Set([20]))
    expect(keys(plegadoB.nodes).sort()).toEqual([nodeKey("cumulo", 30), nodeKey("identidades:org", 3)].sort())
    const sint = plegadoB.edges.find((e) => e.relationType === "agregada")
    expect(sint).toBeDefined()
    expect(sint?.srcSlug === "cumulo" || sint?.dstSlug === "cumulo").toBe(true)
    // B expandido + A plegado → A visible (plegado), x oculto dentro de A.
    const plegadoA = collapseClusters(nodes, edges, new Set([30]))
    expect(keys(plegadoA.nodes)).toContain(nodeKey("cumulo", 20))
    expect(keys(plegadoA.nodes)).toContain(nodeKey("cumulo", 30))
    expect(keys(plegadoA.nodes)).not.toContain(nodeKey("identidades:person", 4))
  })

  it("status agregado: confirmed domina sobre pista", () => {
    const out = collapseClusters(
      [cumulo, m1, m2, fuera],
      [
        miembro(1, m1, cumulo),
        miembro(2, m2, cumulo),
        edge(3, m1, fuera), // pista
        edge(4, m2, fuera, { status: "confirmed", relationType: "contraparte" }),
      ],
      new Set(),
    )
    expect(out.edges).toHaveLength(1)
    expect(out.edges[0].status).toBe("confirmed")
  })

  it("ids sintéticos deterministas: misma entrada → mismos ids", () => {
    const nodes = [cumulo, m1, m2, fuera]
    const edges = [miembro(1, m1, cumulo), miembro(2, m2, cumulo), edge(3, m1, fuera), edge(4, m2, fuera)]
    const a = collapseClusters(nodes, edges, new Set())
    const b = collapseClusters(nodes, edges, new Set())
    expect(a.edges.map((e) => e.id)).toEqual(b.edges.map((e) => e.id))
  })

  it("miembro↔miembro de cúmulos plegados distintos → sintética cúmulo↔cúmulo", () => {
    const otro = node("cumulo", 11, "cumulo")
    const m3 = node("calendar", 5, "evento")
    const out = collapseClusters(
      [cumulo, otro, m1, m3],
      [miembro(1, m1, cumulo), miembro(2, m3, otro), edge(3, m1, m3)],
      new Set(),
    )
    expect(keys(out.nodes).sort()).toEqual([nodeKey("cumulo", 10), nodeKey("cumulo", 11)].sort())
    expect(out.edges).toHaveLength(1)
    const e = out.edges[0]
    expect(e.relationType).toBe("agregada")
    expect(e.srcSlug).toBe("cumulo")
    expect(e.dstSlug).toBe("cumulo")
  })

  it("sin membresías presentes (cúmulo filtrado por la leyenda) no pliega nada", () => {
    const nodes = [m1, m2] // el cúmulo quedó oculto por un filtro previo
    const edges = [edge(3, m1, m2)]
    const out = collapseClusters(nodes, edges, new Set())
    expect(out.nodes).toEqual(nodes)
    expect(out.edges).toEqual(edges)
  })
})
