import { describe, expect, it } from "vitest"
import { flattenStats } from "./worker-stats"

// Regresión del bug que dejaba /pipeline en negro: flattenStats devolvía un objeto como `v` y React
// lanzaba "Objects are not valid as a React child". Cada caso reproduce una forma real de
// worker_runs.stats; la invariante clave es que NINGÚN `v` puede ser un objeto (siempre string).
describe("flattenStats", () => {
  it("deja las stats planas tal cual (ocr/log_purge)", () => {
    expect(flattenStats({ ok: 5, errors: 2 })).toEqual([
      { k: "ok", v: "5" },
      { k: "errors", v: "2" },
    ])
  })

  it("aplana un nivel de anidación con prefijo punteado (classify.by_tier)", () => {
    expect(flattenStats({ scanned: 10, by_tier: { batch: 5, individual: 3 } })).toEqual([
      { k: "scanned", v: "10" },
      { k: "by_tier.batch", v: "5" },
      { k: "by_tier.individual", v: "3" },
    ])
  })

  it("aplana la corrida de reprocess que crasheaba (results.classify anidado)", () => {
    const stats = {
      stages: ["classify"],
      results: { classify: { already: 5, missing: 0, classified: 0 } },
      targets: 5,
    }
    expect(flattenStats(stats)).toEqual([
      { k: "stages", v: "classify" },
      { k: "results.classify.already", v: "5" },
      { k: "results.classify.missing", v: "0" },
      { k: "results.classify.classified", v: "0" },
      { k: "targets", v: "5" },
    ])
  })

  it("aplana 3 niveles sin lanzar (summarize/extract → cost.by_source)", () => {
    const stats = { cost: { by_source: { "1": { cost_usd: 0.42, prompt_tokens: 3000 } } } }
    expect(flattenStats(stats)).toEqual([
      { k: "cost.by_source.1.cost_usd", v: "0.42" },
      { k: "cost.by_source.1.prompt_tokens", v: "3000" },
    ])
  })

  it("junta arrays de primitivos en un solo chip (calendar.steps_failed)", () => {
    expect(flattenStats({ steps_failed: ["pull:2", "merge:no_quota"] })).toEqual([
      { k: "steps_failed", v: "pull:2, merge:no_quota" },
    ])
  })

  it("indexa arrays de objetos por posición", () => {
    expect(flattenStats({ items: [{ a: 1 }, { a: 2 }] })).toEqual([
      { k: "items[0].a", v: "1" },
      { k: "items[1].a", v: "2" },
    ])
  })

  it("serializa hojas no numéricas (reprocess {error}) y null", () => {
    expect(flattenStats({ results: { extract: { error: "boom" } }, x: null })).toEqual([
      { k: "results.extract.error", v: "boom" },
      { k: "x", v: "null" },
    ])
  })

  it("devuelve [] para stats vacías", () => {
    expect(flattenStats({})).toEqual([])
  })

  it("garantiza que ningún valor sea un objeto (la invariante que evita el crash)", () => {
    const stats = {
      stages: ["classify"],
      results: { classify: { already: 5 }, extract: { error: "x" } },
      cost: { by_source: { "1": { cost_usd: 0.1 } } },
      targets: 5,
    }
    for (const chip of flattenStats(stats)) {
      expect(typeof chip.v).toBe("string")
    }
  })
})
