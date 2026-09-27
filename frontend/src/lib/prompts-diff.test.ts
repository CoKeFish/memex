import { describe, expect, it } from "vitest"
import { describeDiff, diffSummary } from "./prompts-diff"

describe("diffSummary", () => {
  it("cuenta líneas nuevas y quitadas, ignora espacios finales y saltos de Windows", () => {
    const d = diffSummary("a\nb\nc", "a\r\nb  \r\nd\r\ne")
    expect(d.added).toEqual(["d", "e"])
    expect(d.removed).toEqual(["c"])
    expect(d.same).toBe(false)
    expect(describeDiff(d)).toContain("2 líneas nuevas, 1 línea quitada")
  })
  it("igual texto = sin cambios", () => {
    const d = diffSummary("hola\nmundo\n", "hola\nmundo")
    expect(d.same).toBe(true)
    expect(describeDiff(d)).toBe("Sin cambios.")
  })
  it("una línea repetida cuenta por cada aparición", () => {
    const d = diffSummary("x\nx", "x")
    expect(d.removed).toEqual(["x"])
    expect(d.added).toEqual([])
  })
})
