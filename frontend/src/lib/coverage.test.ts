import { describe, expect, it } from "vitest"
import {
  addDays,
  axisTicks,
  domainDays,
  markerPosition,
  mergeForWidth,
  segmentPosition,
} from "./coverage"

const D = (min: string, max: string) => ({ min, max })

describe("domainDays", () => {
  it("es inclusive en ambos extremos", () => {
    expect(domainDays(D("2026-06-01", "2026-06-10"))).toBe(10)
  })
  it("min == max → 1", () => {
    expect(domainDays(D("2026-06-01", "2026-06-01"))).toBe(1)
  })
})

describe("addDays", () => {
  it("suma y resta cruzando mes/año", () => {
    expect(addDays("2026-06-10", -90)).toBe("2026-03-12")
    expect(addDays("2026-01-01", -1)).toBe("2025-12-31")
    expect(addDays("2026-02-27", 2)).toBe("2026-03-01")
    expect(addDays("2026-06-10", 0)).toBe("2026-06-10")
  })
})

describe("markerPosition", () => {
  const dom = D("2026-06-01", "2026-06-10") // 10 días

  it("marca el FINAL del día (día 5 de 10 → 50%)", () => {
    expect(markerPosition("2026-06-05", dom)).toBe(50)
  })

  it("último día del dominio → 100", () => {
    expect(markerPosition("2026-06-10", dom)).toBe(100)
  })

  it("nunca pasa de 100", () => {
    expect(markerPosition("2026-06-25", dom)).toBe(100)
  })
})

describe("segmentPosition", () => {
  const dom = D("2026-06-01", "2026-06-10") // 10 días

  it("rango interior: días 3-4 de 10 → left 20%, width 20%", () => {
    expect(segmentPosition({ start: "2026-06-03", end: "2026-06-04" }, dom)).toEqual({
      leftPct: 20,
      widthPct: 20,
    })
  })

  it("un día al inicio → left 0%, width 10%", () => {
    expect(segmentPosition({ start: "2026-06-01", end: "2026-06-01" }, dom)).toEqual({
      leftPct: 0,
      widthPct: 10,
    })
  })

  it("rango == dominio → 0% / 100%", () => {
    expect(segmentPosition({ start: "2026-06-01", end: "2026-06-10" }, dom)).toEqual({
      leftPct: 0,
      widthPct: 100,
    })
  })
})

describe("mergeForWidth", () => {
  const dom = D("2024-01-01", "2025-12-31") // 731 días
  const ranges = [
    { start: "2024-02-01", end: "2024-02-10", count: 5 },
    { start: "2024-02-12", end: "2024-02-20", count: 3 }, // hueco de 1 día (el 11)
  ]

  it("a 300px el hueco proyecta <1px → se funden con counts sumados", () => {
    const segs = mergeForWidth(ranges, dom, 300)
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({
      start: "2024-02-01",
      end: "2024-02-20",
      count: 8,
      days: 20,
      merged: 2,
    })
  })

  it("a 3000px el hueco proyecta >=1px → quedan separados", () => {
    const segs = mergeForWidth(ranges, dom, 3000)
    expect(segs).toHaveLength(2)
    expect(segs.map((s) => s.merged)).toEqual([1, 1])
  })

  it("lista vacía → []", () => {
    expect(mergeForWidth([], dom, 300)).toEqual([])
  })

  it("no muta los rangos de entrada al fundir", () => {
    mergeForWidth(ranges, dom, 300)
    expect(ranges[0]).toEqual({ start: "2024-02-01", end: "2024-02-10", count: 5 })
  })
})

describe("axisTicks", () => {
  it("dominio de ~6 meses → primeros de mes, pct creciente", () => {
    const ticks = axisTicks(D("2026-01-15", "2026-07-15"))
    expect(ticks.map((t) => t.day)).toEqual([
      "2026-02-01",
      "2026-03-01",
      "2026-04-01",
      "2026-05-01",
      "2026-06-01",
      "2026-07-01",
    ])
    for (let i = 1; i < ticks.length; i++) {
      expect(ticks[i].pct).toBeGreaterThan(ticks[i - 1].pct)
    }
  })

  it("el label de 2026-02-01 es febrero (sin off-by-one UTC)", () => {
    const ticks = axisTicks(D("2026-01-15", "2026-07-15"))
    expect(ticks[0].label).toMatch(/feb/i)
  })

  it("dominio de 10 años → eneros con label de año", () => {
    const ticks = axisTicks(D("2016-03-10", "2026-06-01"))
    expect(ticks.length).toBeLessThanOrEqual(12)
    expect(ticks.every((t) => t.day.endsWith("-01-01"))).toBe(true)
    expect(ticks[0].label).toBe("2017")
  })

  it("dominio sin ningún 1ro de mes adentro → extremos del dominio", () => {
    const ticks = axisTicks(D("2026-06-05", "2026-06-20"))
    expect(ticks.map((t) => t.day)).toEqual(["2026-06-05", "2026-06-20"])
  })
})
