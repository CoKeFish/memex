import { describe, expect, it } from "vitest"
import { runRequestMatchesLot, type ProcessingRunRequest } from "./processing"

function req(over: Partial<ProcessingRunRequest> = {}): ProcessingRunRequest {
  return {
    stages: ["classify", "summarize"],
    sourceId: null,
    since: null,
    until: null,
    limit: null,
    only: null,
    force: false,
    ...over,
  }
}

const LOT = {
  stages: ["summarize", "classify"], // el backend reordena a STAGE_ORDER: comparar como conjunto
  filters: { source_id: null, since: null, until: null, limit: null, only: null },
  force: false,
}

describe("runRequestMatchesLot · divergencia form ↔ lote congelado", () => {
  it("coincide ignorando el orden de etapas", () => {
    expect(runRequestMatchesLot(req(), LOT)).toBe(true)
  })

  it("filtros ausentes en el eco del lote cuentan como null", () => {
    expect(runRequestMatchesLot(req(), { stages: ["classify", "summarize"], filters: {}, force: false })).toBe(
      true,
    )
  })

  it("detecta cada campo divergente", () => {
    expect(runRequestMatchesLot(req({ stages: ["classify"] }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ sourceId: 3 }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ since: "2026-01-01" }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ until: "2026-02-01" }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ limit: 100 }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ only: "errored" }), LOT)).toBe(false)
    expect(runRequestMatchesLot(req({ force: true }), LOT)).toBe(false)
  })

  it("coincide con el eco completo de un lote acotado", () => {
    const r = req({ sourceId: 14, since: "2026-05-01", limit: 200, only: "errored" })
    const lot = {
      stages: ["classify", "summarize"],
      filters: { source_id: 14, since: "2026-05-01", until: null, limit: 200, only: "errored" },
      force: false,
    }
    expect(runRequestMatchesLot(r, lot)).toBe(true)
  })
})
