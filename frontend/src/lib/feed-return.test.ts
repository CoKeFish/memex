import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { consumeFeedReturn, loadFeedReturn, saveFeedReturn } from "./feed-return"

// Entorno node sin DOM: se stubbea sessionStorage en globalThis (Map mínimo).
function stubStorage(overrides: Partial<Storage> = {}): Map<string, string> {
  const store = new Map<string, string>()
  const stub = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    ...overrides,
  }
  ;(globalThis as Record<string, unknown>).sessionStorage = stub
  return store
}

describe("feed-return", () => {
  beforeEach(() => stubStorage())
  afterEach(() => {
    delete (globalThis as Record<string, unknown>).sessionStorage
  })

  it("roundtrip save → load (load NO consume)", () => {
    const state = { search: "source=3&q=x", anchorKey: "r:42", anchorDelta: 12, scrollTop: 980 }
    saveFeedReturn(state)
    expect(loadFeedReturn()).toEqual(state)
    expect(loadFeedReturn()).toEqual(state)
  })

  it("consume es one-shot", () => {
    saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0 })
    expect(consumeFeedReturn()).not.toBeNull()
    expect(consumeFeedReturn()).toBeNull()
    expect(loadFeedReturn()).toBeNull()
  })

  it("JSON corrupto o shape inválido → null", () => {
    const store = stubStorage()
    store.set("memex.feed.return", "{no es json")
    expect(loadFeedReturn()).toBeNull()
    store.set("memex.feed.return", JSON.stringify({ anchorKey: "r:1" })) // sin `search`
    expect(loadFeedReturn()).toBeNull()
  })

  it("campos faltantes o de tipo errado se normalizan con defaults", () => {
    const store = stubStorage()
    store.set("memex.feed.return", JSON.stringify({ search: "q=a", anchorDelta: "12" }))
    expect(loadFeedReturn()).toEqual({ search: "q=a", anchorKey: null, anchorDelta: 0, scrollTop: 0 })
  })

  it("storage que lanza no explota (modo privado / cuota)", () => {
    stubStorage({
      setItem: () => {
        throw new Error("QuotaExceeded")
      },
      getItem: () => {
        throw new Error("denied")
      },
    })
    expect(() => saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0 })).not.toThrow()
    expect(loadFeedReturn()).toBeNull()
    expect(consumeFeedReturn()).toBeNull()
  })

  it("sessionStorage ausente (node puro) degrada a null", () => {
    delete (globalThis as Record<string, unknown>).sessionStorage
    expect(() => saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0 })).not.toThrow()
    expect(loadFeedReturn()).toBeNull()
  })
})
