import { describe, expect, it } from "vitest"
import type { GraphEdge, GraphNode } from "@/data/graph"
import { baseRadius, connectedComponents, layoutGraph, nodeKey, shelfPack } from "./graph-layout"

function node(slug: string, id: number, kind = "persona"): GraphNode {
  return { slug, id, label: `${slug}#${id}`, kind, sourceInboxIds: [] }
}

function edge(id: number, a: GraphNode, b: GraphNode): GraphEdge {
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
  }
}

describe("connectedComponents", () => {
  it("separa triángulo + par + aislado con la membresía correcta", () => {
    const [a, b, c, d, e, f] = [1, 2, 3, 4, 5, 6].map((i) => node("identidades:person", i))
    const edges = [edge(1, a, b), edge(2, b, c), edge(3, a, c), edge(4, d, e)]
    const comps = connectedComponents([a, b, c, d, e, f], edges)
    expect(comps.map((x) => x.nodes.length)).toEqual([3, 2, 1])
    expect(comps[0].edges).toHaveLength(3)
    expect(comps[1].nodes.map((n) => n.id)).toEqual([4, 5])
    expect(comps[2].nodes.map((n) => n.id)).toEqual([6])
  })

  it("ignora aristas con un extremo fuera del set de nodos", () => {
    const a = node("finance", 1)
    const fantasma = node("calendar", 99)
    const comps = connectedComponents([a], [edge(1, a, fantasma)])
    expect(comps).toHaveLength(1)
    expect(comps[0].edges).toHaveLength(0)
  })
})

describe("shelfPack", () => {
  it("sin solapes par a par y offsets no negativos", () => {
    const boxes = [
      { w: 120, h: 90 },
      { w: 80, h: 80 },
      { w: 60, h: 60 },
      { w: 40, h: 40 },
      { w: 40, h: 40 },
      { w: 30, h: 30 },
    ]
    const gap = 10
    const off = shelfPack(boxes, gap, 5 / 3)
    expect(off).toHaveLength(boxes.length)
    for (const o of off) {
      expect(o.x).toBeGreaterThanOrEqual(0)
      expect(o.y).toBeGreaterThanOrEqual(0)
    }
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const sep =
          off[i].x + boxes[i].w <= off[j].x ||
          off[j].x + boxes[j].w <= off[i].x ||
          off[i].y + boxes[i].h <= off[j].y ||
          off[j].y + boxes[j].h <= off[i].y
        expect(sep).toBe(true)
      }
    }
  })

  it("una caja más ancha que el techo entra igual (fila propia)", () => {
    const off = shelfPack([{ w: 500, h: 10 }, { w: 20, h: 10 }], 5, 1)
    expect(off[0]).toEqual({ x: 0, y: 0 })
    expect(off[1].x === 0 || off[1].x >= 500).toBe(true)
  })
})

describe("layoutGraph", () => {
  const triangulo = () => {
    const [a, b, c] = [1, 2, 3].map((i) => node("identidades:person", i))
    return { nodes: [a, b, c], edges: [edge(1, a, b), edge(2, b, c), edge(3, a, c)] }
  }

  it("es determinista: dos corridas con el mismo input dan coordenadas idénticas", () => {
    const { nodes, edges } = triangulo()
    const d = node("finance", 9)
    const e = node("calendar", 9)
    const all = [...nodes, d, e]
    const allEdges = [...edges, edge(4, d, e)]
    const l1 = layoutGraph(all, allEdges)
    const l2 = layoutGraph(all, allEdges)
    expect(l1.sims.map((s) => [s.key, s.x, s.y])).toEqual(l2.sims.map((s) => [s.key, s.x, s.y]))
  })

  it("dos componentes no se solapan (bboxes disjuntos)", () => {
    const { nodes, edges } = triangulo()
    const d = node("finance", 9)
    const e = node("calendar", 9)
    const lay = layoutGraph([...nodes, d, e], [...edges, edge(4, d, e)])
    const bbox = (keys: string[]) => {
      const pts = lay.sims.filter((s) => keys.includes(s.key))
      return {
        minX: Math.min(...pts.map((p) => p.x)),
        maxX: Math.max(...pts.map((p) => p.x)),
        minY: Math.min(...pts.map((p) => p.y)),
        maxY: Math.max(...pts.map((p) => p.y)),
      }
    }
    const b1 = bbox(nodes.map((n) => nodeKey(n.slug, n.id)))
    const b2 = bbox([nodeKey("finance", 9), nodeKey("calendar", 9)])
    const disjoint =
      b1.maxX < b2.minX || b2.maxX < b1.minX || b1.maxY < b2.minY || b2.maxY < b1.minY
    expect(disjoint).toBe(true)
  })

  it("input vacío → bounds fallback (contrato de fitView)", () => {
    const lay = layoutGraph([], [])
    expect(lay.sims).toEqual([])
    expect(lay.bounds).toEqual({ minX: -100, minY: -100, maxX: 100, maxY: 100 })
  })

  it("solo aislados → coordenadas finitas y sin solapes (alfombra)", () => {
    const nodes = Array.from({ length: 12 }, (_, i) => node("calendar", i + 1, "evento"))
    const lay = layoutGraph(nodes, [])
    expect(lay.sims).toHaveLength(12)
    for (const s of lay.sims) {
      expect(Number.isFinite(s.x)).toBe(true)
      expect(Number.isFinite(s.y)).toBe(true)
    }
    for (let i = 0; i < lay.sims.length; i++) {
      for (let j = i + 1; j < lay.sims.length; j++) {
        const a = lay.sims[i]
        const b = lay.sims[j]
        const d = Math.hypot(a.x - b.x, a.y - b.y)
        expect(d).toBeGreaterThanOrEqual(baseRadius(a.node, 0) + baseRadius(b.node, 0))
      }
    }
  })

  it("los nodos conectados quedan separados (forceCollide)", () => {
    const { nodes, edges } = triangulo()
    const lay = layoutGraph(nodes, edges)
    for (let i = 0; i < lay.sims.length; i++) {
      for (let j = i + 1; j < lay.sims.length; j++) {
        const a = lay.sims[i]
        const b = lay.sims[j]
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(
          baseRadius(a.node, a.deg) + baseRadius(b.node, b.deg) - 1,
        )
      }
    }
  })
})
