import { afterEach, beforeEach, describe, expect, it } from "vitest"
import {
  consumeFeedReturn,
  feedSourceIdFromSearch,
  loadFeedReturn,
  retargetFeedReturn,
  saveFeedReturn,
} from "./feed-return"

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
    const state = { search: "source=3&q=x", anchorKey: "r:42", anchorDelta: 12, scrollTop: 980, pages: 3 }
    saveFeedReturn(state)
    expect(loadFeedReturn()).toEqual(state)
    expect(loadFeedReturn()).toEqual(state)
  })

  it("consume es one-shot", () => {
    saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0, pages: 1 })
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
    expect(loadFeedReturn()).toEqual({
      search: "q=a",
      anchorKey: null,
      anchorDelta: 0,
      scrollTop: 0,
      pages: 1,
    })
  })

  it("`pages` se normaliza: entero ≥ 1 (estados viejos sin el campo → 1)", () => {
    const store = stubStorage()
    store.set("memex.feed.return", JSON.stringify({ search: "", pages: 2.7 }))
    expect(loadFeedReturn()?.pages).toBe(2)
    store.set("memex.feed.return", JSON.stringify({ search: "", pages: 0 }))
    expect(loadFeedReturn()?.pages).toBe(1)
    store.set("memex.feed.return", JSON.stringify({ search: "", pages: "3" }))
    expect(loadFeedReturn()?.pages).toBe(1)
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
    expect(() => saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0, pages: 1 })).not.toThrow()
    expect(loadFeedReturn()).toBeNull()
    expect(consumeFeedReturn()).toBeNull()
  })

  it("sessionStorage ausente (node puro) degrada a null", () => {
    delete (globalThis as Record<string, unknown>).sessionStorage
    expect(() => saveFeedReturn({ search: "", anchorKey: null, anchorDelta: 0, scrollTop: 0, pages: 1 })).not.toThrow()
    expect(loadFeedReturn()).toBeNull()
  })
})

describe("feedSourceIdFromSearch / retargetFeedReturn — navegación entre mensajes", () => {
  beforeEach(() => stubStorage())
  afterEach(() => {
    delete (globalThis as Record<string, unknown>).sessionStorage
  })

  it("lee la fuente activa del search del feed; 'all', vacío o basura → undefined", () => {
    expect(feedSourceIdFromSearch("source=3&q=x")).toBe(3)
    expect(feedSourceIdFromSearch("q=x")).toBeUndefined()
    expect(feedSourceIdFromSearch("source=all")).toBeUndefined()
    expect(feedSourceIdFromSearch("source=abc")).toBeUndefined()
    expect(feedSourceIdFromSearch("")).toBeUndefined()
    expect(feedSourceIdFromSearch(null)).toBeUndefined()
  })

  it("retarget mueve el ancla al mensaje al que se saltó y conserva filtro y páginas", () => {
    saveFeedReturn({ search: "source=3", anchorKey: "r:10", anchorDelta: 40, scrollTop: 900, pages: 2 })
    retargetFeedReturn(55)
    expect(loadFeedReturn()).toEqual({
      search: "source=3",
      anchorKey: "r:55",
      anchorDelta: 0,
      scrollTop: 900,
      pages: 2,
    })
  })

  it("retarget sin estado guardado no inventa uno", () => {
    retargetFeedReturn(55)
    expect(loadFeedReturn()).toBeNull()
  })
})
