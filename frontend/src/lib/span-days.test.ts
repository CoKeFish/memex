import { describe, expect, it } from "vitest"
import { spanDays } from "./span-days"

describe("spanDays", () => {
  it("un evento de un dia ocupa solo su inicio", () => {
    expect(spanDays("2026-09-09", null)).toEqual(["2026-09-09"])
    expect(spanDays("2026-09-09", "2026-09-09")).toEqual(["2026-09-09"])
  })
  it("un evento multi-dia ocupa todos sus dias (inclusive), cruzando el mes", () => {
    expect(spanDays("2026-09-07", "2026-09-11")).toEqual([
      "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
    ])
    expect(spanDays("2026-08-31", "2026-09-02")).toEqual(["2026-08-31", "2026-09-01", "2026-09-02"])
  })
  it("un rango absurdo se acota", () => {
    expect(spanDays("2026-01-01", "2030-01-01").length).toBe(62)
  })
})
