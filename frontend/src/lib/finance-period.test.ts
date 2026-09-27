import { describe, expect, it } from "vitest"
import type { FinanceTransaction } from "@/types/domain"
import {
  applyFilters,
  byCategory,
  byMerchant,
  coverage,
  EMPTY_FILTERS,
  monthInSentence,
  monthLongLabel,
  monthRange,
  monthSeries,
  monthShortLabel,
  monthsWithData,
  monthsWithStatement,
  pace,
  periodMonths,
  rowsInPeriod,
  shiftMonth,
  summarize,
} from "./finance-period"

function tx(over: Partial<FinanceTransaction> & { id: number; occurredOn: string }): FinanceTransaction {
  return {
    direction: "egreso",
    status: "completada",
    kind: "pago",
    amount: 1000,
    currency: "COP",
    merchant: "Uber",
    counterparty: "Uber",
    category: "transporte",
    description: "",
    evidence: "",
    sourceInboxIds: [],
    createdAt: "2026-09-01T00:00:00Z",
    placeName: null,
    placeAddress: null,
    fromDocument: false,
    ...over,
  }
}

describe("periodo", () => {
  it("shiftMonth cruza el año en los dos sentidos", () => {
    expect(shiftMonth("2026-01", -1)).toBe("2025-12")
    expect(shiftMonth("2025-12", 2)).toBe("2026-02")
    expect(shiftMonth("2026-09", 0)).toBe("2026-09")
  })

  it("periodMonths devuelve el tramo que termina en el mes ancla", () => {
    expect(periodMonths({ mode: "mes", month: "2026-09" }, [])).toEqual(["2026-09"])
    expect(periodMonths({ mode: "3m", month: "2026-01" }, [])).toEqual(["2025-11", "2025-12", "2026-01"])
    expect(periodMonths({ mode: "12m", month: "2026-09" }, [])).toHaveLength(12)
  })

  it("«todo» usa los meses con datos, ordenados", () => {
    const meses = periodMonths({ mode: "todo", month: "2026-09" }, ["2026-03", "2025-12"])
    expect(meses).toEqual(["2025-12", "2026-03"])
  })

  it("monthRange rellena los meses vacíos del medio", () => {
    expect(monthRange("2026-07", "2026-10")).toEqual(["2026-07", "2026-08", "2026-09", "2026-10"])
    expect(monthRange("2026-10", "2026-07")).toEqual([])
  })

  it("monthsWithData mira solo la moneda pedida", () => {
    const txns = [
      tx({ id: 1, occurredOn: "2026-09-03" }),
      tx({ id: 2, occurredOn: "2026-07-30", currency: "USD" }),
    ]
    expect(monthsWithData(txns, "COP")).toEqual(["2026-09"])
    expect(monthsWithData(txns)).toEqual(["2026-07", "2026-09"])
  })

  it("rótulos de mes en español y sin puntos", () => {
    expect(monthShortLabel("2026-09")).toBe("sep 26")
    expect(monthLongLabel("2026-09")).toBe("Septiembre 2026")
  })

  it("dentro de una frase, el año solo si no es el actual", () => {
    const hoy = new Date(2026, 8, 14)
    expect(monthInSentence("2026-09", hoy)).toBe("septiembre")
    expect(monthInSentence("2025-12", hoy)).toBe("diciembre de 2025")
  })
})

describe("summarize", () => {
  const rows = [
    tx({ id: 1, occurredOn: "2026-09-02", amount: 24139 }),
    tx({ id: 2, occurredOn: "2026-09-03", amount: 84000, direction: "ingreso", category: "otros" }),
    tx({ id: 3, occurredOn: "2026-09-04", amount: 5000, direction: "ingreso", kind: "reembolso" }),
    tx({ id: 4, occurredOn: "2026-09-05", amount: 99999, status: "fallida" }),
    tx({ id: 5, occurredOn: "2026-09-06", amount: 12345, status: "pendiente" }),
  ]

  it("un reembolso descuenta del gasto y no es ingreso; lo que no se movió no suma", () => {
    const s = summarize(rows)
    expect(s.income).toBe(84000)
    expect(s.expense).toBe(24139 - 5000)
    expect(s.balance).toBe(84000 - 19139)
    expect(s.refunded).toBe(5000)
    expect(s.count).toBe(3)
    expect(s.unsettled).toBe(2)
  })

  it("sin filas, todo en cero", () => {
    expect(summarize([])).toMatchObject({ income: 0, expense: 0, balance: 0, count: 0 })
  })
})

describe("monthSeries", () => {
  it("un punto por mes pedido, con ceros donde no hubo nada", () => {
    const txns = [
      tx({ id: 1, occurredOn: "2026-07-10", amount: 3000 }),
      tx({ id: 2, occurredOn: "2026-09-10", amount: 1000 }),
      tx({ id: 3, occurredOn: "2026-09-11", amount: 7000, direction: "ingreso" }),
      tx({ id: 4, occurredOn: "2026-09-12", amount: 500, currency: "USD" }),
    ]
    const serie = monthSeries(txns, "COP", ["2026-07", "2026-08", "2026-09"])
    expect(serie.map((p) => p.key)).toEqual(["2026-07", "2026-08", "2026-09"])
    expect(serie[0].expense).toBe(3000)
    expect(serie[1]).toMatchObject({ expense: 0, income: 0, count: 0 })
    expect(serie[2]).toMatchObject({ expense: 1000, income: 7000, balance: 6000 })
  })
})

describe("pace", () => {
  const txns = [
    // mes en curso: 3.000 hasta el día 10
    tx({ id: 1, occurredOn: "2026-09-02", amount: 1000 }),
    tx({ id: 2, occurredOn: "2026-09-09", amount: 2000 }),
    tx({ id: 3, occurredOn: "2026-09-20", amount: 9999 }), // después del día de hoy: fuera
    // agosto: 4.000 hasta el día 10 (y más después)
    tx({ id: 4, occurredOn: "2026-08-01", amount: 4000 }),
    tx({ id: 5, occurredOn: "2026-08-25", amount: 50000 }),
    // julio: 6.000 hasta el día 10
    tx({ id: 6, occurredOn: "2026-07-10", amount: 6000 }),
  ]

  it("compara el mes en curso contra el mismo día de los meses anteriores", () => {
    const p = pace(txns, "COP", "2026-09", new Date(2026, 8, 10), 2)
    expect(p).not.toBeNull()
    expect(p!.day).toBe(10)
    expect(p!.days).toBe(30)
    expect(p!.spent).toBe(3000)
    expect(p!.average).toBe(5000) // (4000 + 6000) / 2
    expect(p!.basis).toBe(2)
  })

  it("no aplica a un mes que ya terminó", () => {
    expect(pace(txns, "COP", "2026-08", new Date(2026, 8, 10), 2)).toBeNull()
  })
})

describe("cortes y filtros", () => {
  const rows = [
    tx({ id: 1, occurredOn: "2026-09-02", amount: 24139, merchant: "Uber" }),
    tx({ id: 2, occurredOn: "2026-09-03", amount: 8000, merchant: "Tigo", category: "servicios" }),
    tx({ id: 3, occurredOn: "2026-09-04", amount: 4139, merchant: "Uber" }),
    tx({ id: 4, occurredOn: "2026-09-05", amount: 5000, merchant: "Uber", kind: "reembolso", direction: "ingreso" }),
    tx({ id: 5, occurredOn: "2026-09-06", amount: 90000, direction: "ingreso", category: "otros", merchant: "Nequi" }),
    tx({ id: 6, occurredOn: "2026-09-07", amount: 70000, status: "fallida", merchant: "Railway" }),
  ]

  it("byCategory ordena por gasto y descuenta los reembolsos", () => {
    const cats = byCategory(rows)
    expect(cats.map((c) => c.category)).toEqual(["transporte", "servicios"])
    expect(cats[0].total).toBe(24139 + 4139 - 5000)
    expect(cats[0].count).toBe(3)
  })

  it("byMerchant ignora ingresos y lo que no se movió", () => {
    const m = byMerchant(rows)
    expect(m.map((x) => x.merchant)).toEqual(["Uber", "Tigo"])
    expect(m[0].total).toBe(23278)
  })

  it("applyFilters combina sentido, categoría, texto y estado", () => {
    expect(applyFilters(rows, EMPTY_FILTERS)).toHaveLength(6)
    expect(applyFilters(rows, { ...EMPTY_FILTERS, includeUnsettled: false })).toHaveLength(5)
    expect(applyFilters(rows, { ...EMPTY_FILTERS, direction: "ingreso" })).toHaveLength(2)
    expect(applyFilters(rows, { ...EMPTY_FILTERS, category: "servicios" })).toHaveLength(1)
    expect(applyFilters(rows, { ...EMPTY_FILTERS, query: "uber" })).toHaveLength(3)
    expect(
      applyFilters(rows, { ...EMPTY_FILTERS, query: "UBER", direction: "egreso" }),
    ).toHaveLength(2)
  })
})

describe("rowsInPeriod", () => {
  it("recorta por moneda y por los meses del periodo", () => {
    const txns = [
      tx({ id: 1, occurredOn: "2026-08-31" }),
      tx({ id: 2, occurredOn: "2026-09-01" }),
      tx({ id: 3, occurredOn: "2026-09-02", currency: "USD" }),
    ]
    expect(rowsInPeriod(txns, "COP", ["2026-09"]).map((t) => t.id)).toEqual([2])
    expect(rowsInPeriod(txns, "COP", ["2026-08", "2026-09"]).map((t) => t.id)).toEqual([1, 2])
  })
})


describe("cobertura del extracto", () => {
  const hoy = new Date(2026, 8, 14) // 14 de septiembre
  const txns = [
    tx({ id: 1, occurredOn: "2026-07-10", fromDocument: true }),
    tx({ id: 2, occurredOn: "2026-07-11" }),
    tx({ id: 3, occurredOn: "2026-08-02" }), // agosto: solo correo
    tx({ id: 4, occurredOn: "2026-09-02" }), // mes en curso: aún no hay extracto
    tx({ id: 5, occurredOn: "2026-06-02", fromDocument: true, currency: "USD" }),
  ]

  it("monthsWithStatement mira la moneda y el origen", () => {
    expect([...monthsWithStatement(txns, "COP")]).toEqual(["2026-07"])
    expect([...monthsWithStatement(txns, "USD")]).toEqual(["2026-06"])
  })

  it("señala los meses cerrados sin extracto y aparta el mes en curso", () => {
    const c = coverage(["2026-07", "2026-08", "2026-09"], monthsWithStatement(txns, "COP"), hoy)
    expect(c.missing).toEqual(["2026-08"])
    expect(c.currentMonth).toBe(true)
    expect(c.comparable).toBe(true)
  })

  it("sin ningún extracto cargado no afirma que falte nada", () => {
    const c = coverage(["2026-08"], new Set<string>(), hoy)
    expect(c.comparable).toBe(false)
  })
})
