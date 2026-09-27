import { describe, expect, it } from "vitest"
import {
  CALENDAR_FILTER_STORAGE_KEY,
  applyCalendarFilter,
  rememberCalendarFilter,
  rememberedCalendarFilter,
} from "./calendar-filter"
import type { ConsolidatedEvent } from "@/types/domain"

function ev(id: number, commitment?: "confirmado" | "provisional" | "anuncio"): ConsolidatedEvent {
  return {
    id, title: `e${id}`, startsOn: "2026-09-09", endsOn: null, startTime: null, endTime: null,
    location: "", description: "", placeName: null, placeAddress: null, memberCount: 1,
    origins: ["extraction"], protected: false, priorityRank: 0, members: [],
    commitment,
  }
}

describe("applyCalendarFilter", () => {
  const all = [ev(1, "confirmado"), ev(2, "provisional"), ev(3, "anuncio"), ev(4)]
  it("todo devuelve todo", () => {
    expect(applyCalendarFilter(all, "todos")).toHaveLength(4)
  })
  it("mis eventos = confirmados + provisionales", () => {
    expect(applyCalendarFilter(all, "mios").map((e) => e.id)).toEqual([1, 2])
  })
  it("un estado exacto; sin estado cuenta como anuncio", () => {
    expect(applyCalendarFilter(all, "confirmado").map((e) => e.id)).toEqual([1])
    expect(applyCalendarFilter(all, "anuncio").map((e) => e.id)).toEqual([3, 4])
  })
})

describe("filtro con el que abre /calendario", () => {
  function mem(initial: Record<string, string> = {}) {
    const m = new Map(Object.entries(initial))
    return {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
    }
  }
  it("por default «Mis eventos»: los anuncios no son su calendario hasta que sean un plan", () => {
    expect(rememberedCalendarFilter(mem())).toBe("mios")
    expect(rememberedCalendarFilter(null)).toBe("mios")
  })
  it("recuerda la elección y descarta valores raros", () => {
    const s = mem()
    rememberCalendarFilter(s, "anuncio")
    expect(s.getItem(CALENDAR_FILTER_STORAGE_KEY)).toBe("anuncio")
    expect(rememberedCalendarFilter(s)).toBe("anuncio")
    expect(rememberedCalendarFilter(mem({ [CALENDAR_FILTER_STORAGE_KEY]: "rarísimo" }))).toBe("mios")
  })
  it("sin storage o con storage roto no falla", () => {
    const roto = {
      getItem: () => {
        throw new Error("bloqueado")
      },
      setItem: () => {
        throw new Error("bloqueado")
      },
    }
    expect(rememberedCalendarFilter(roto)).toBe("mios")
    expect(() => rememberCalendarFilter(roto, "todos")).not.toThrow()
  })
})
